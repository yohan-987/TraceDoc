import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import type { ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, ForensicSignal, Region } from "../../../contracts/types";
import { DEFAULT_EVENT_CONFIG, categoryOfDetectors, groupEvents, iou } from "./eventGrouper";

const fx = resolve(dirname(fileURLToPath(import.meta.url)), "../../../contracts/fixtures");
const readJson = <T>(p: string): T => JSON.parse(readFileSync(resolve(fx, p), "utf8")) as T;
const CASES: Record<string, string[]> = { "CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"] };

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object") { Object.values(o as object).forEach(deepFreeze); Object.freeze(o); }
  return o;
}

let n = 0;
type Box = Region["bbox"];
function sig(detector: string, boxes: Box[], confidence = 0.5, page = 1, id?: string): ForensicSignal {
  n++;
  return {
    signalId: id ?? `S${n}`, detector, type: "T", confidence, measurements: {},
    regions: boxes.map((bbox) => ({ docId: "D", page, bbox })),
    provenance: { analyzer: detector, analyzerVersion: "0", config: {}, inputSha256: "0".repeat(64), processedAt: "t", status: "AVAILABLE" },
  };
}
const doc = (signals: ForensicSignal[], docId = "D") => ({ docId, signals });
const group = (signals: ForensicSignal[], opts = {}) => groupEvents([doc(signals)], "C", opts);

// two boxes with a known IoU: A = [0,0,.2,.1], shifted right by dx
const A: Box = [0.1, 0.1, 0.2, 0.1];
const shifted = (dx: number): Box => [A[0] + dx, A[1], A[2], A[3]];

describe("fixtures", () => {
  for (const [caseId, docs] of Object.entries(CASES)) {
    it(`${caseId}: signals group into the fixture events`, () => {
      const a = docs.map((d) => readJson<AnalyzeResponse>(`analyze/${d}.json`));
      const sha: Record<string, string> = {};
      a.forEach((x) => { sha[x.docId] = x.provenance[0]?.inputSha256 ?? ""; });
      const got = groupEvents(a.map((x) => ({ docId: x.docId, signals: x.signals })), caseId, { docSha256: sha, processedAt: "2026-11-01T09:00:00Z" });
      assert.deepStrictEqual(got, readJson<ReasoningOutput>(`${caseId}.reasoning.json`).events);
    });
  }
  it("CASE-001: ELA and noise on the amount region are ONE event, the text-layer signal is another", () => {
    const a = (CASES["CASE-001"] ?? []).map((d) => readJson<AnalyzeResponse>(`analyze/${d}.json`));
    const ev = groupEvents(a.map((x) => ({ docId: x.docId, signals: x.signals })), "CASE-001");
    assert.equal(ev.length, 2);
    assert.deepEqual(ev[0]?.detectors, ["image.ela", "image.noise"]);
    assert.equal(ev[0]?.regions.length, 1);
    assert.equal(ev[0]?.confidence, 0.67);
  });
});

describe("clustering", () => {
  it("three overlapping signals count as one event", () => {
    const ev = group([sig("image.ela", [A], 0.6), sig("image.noise", [shifted(0.02)], 0.5), sig("image.copymove", [shifted(0.04)], 0.4)]);
    assert.equal(ev.length, 1);
    assert.equal(ev[0]?.signalIds.length, 3);
  });
  it("is transitive: A overlaps B and B overlaps C, but A and C do not overlap each other", () => {
    const a = sig("image.ela", [A]), b = sig("image.noise", [shifted(0.06)]), c = sig("image.copymove", [shifted(0.12)]);
    assert.ok(iou(A, shifted(0.12)) < 0.3 && iou(A, shifted(0.06)) >= 0.3 && iou(shifted(0.06), shifted(0.12)) >= 0.3);
    assert.equal(group([a, b, c]).length, 1);
  });
  it("signals that do not overlap stay separate events, numbered by first signal", () => {
    const ev = group([sig("image.ela", [A]), sig("image.ela", [[0.6, 0.6, 0.1, 0.1]])]);
    assert.deepEqual(ev.map((e) => e.eventId), ["EV-001", "EV-002"]);
  });
  it("the same box on different pages is not an overlap", () => {
    assert.equal(group([sig("image.ela", [A], 0.5, 1), sig("image.noise", [A], 0.5, 2)]).length, 2);
  });
  it("signals of different documents are never grouped", () => {
    const x = { docId: "D1", signals: [sig("image.ela", [A])] }, y = { docId: "D2", signals: [sig("image.noise", [A])] };
    const ev = groupEvents([x, y], "C");
    assert.deepEqual(ev.map((e) => [e.docId, e.eventId]), [["D1", "EV-001"], ["D2", "EV-002"]]);
  });
  it("IoU exactly at the threshold groups (>=), just below does not, and the threshold is configurable", () => {
    // identical widths: IoU = (w - dx) / (w + dx). dx = 0.2 * 7/13 gives 0.3 exactly in the limit; test with simple numbers instead
    const b1: Box = [0, 0, 1, 1], b2: Box = [0.5, 0, 1, 1]; // overlap 0.5, union 1.5 => 1/3
    assert.ok(Math.abs(iou(b1, b2) - 1 / 3) < 1e-12);
    assert.equal(group([sig("a", [b1]), sig("b", [b2])]).length, 1); // 0.333 >= 0.3
    assert.equal(group([sig("a", [b1]), sig("b", [b2])], { config: { ...DEFAULT_EVENT_CONFIG, iouThreshold: 0.34 } }).length, 2);
    const eq = group([sig("a", [[0, 0, 1, 1]]), sig("b", [[0, 0, 1, 1]])], { config: { ...DEFAULT_EVENT_CONFIG, iouThreshold: 1 } });
    assert.equal(eq.length, 1, "IoU 1 >= threshold 1");
  });
  it("touching edges and degenerate boxes have IoU 0", () => {
    assert.equal(iou([0, 0, 0.5, 0.5], [0.5, 0, 0.5, 0.5]), 0);
    assert.equal(iou([0, 0, 0, 0], [0, 0, 0, 0]), 0);
  });
});

describe("event confidence", () => {
  const conf = (signals: ForensicSignal[], opts = {}) => group(signals, opts)[0]?.confidence;
  it("is the max member confidence for a single signal", () => assert.equal(conf([sig("image.ela", [A], 0.62)]), 0.62));
  it("adds the bonus for each additional distinct detector family", () => {
    assert.equal(conf([sig("image.ela", [A], 0.62), sig("image.noise", [A], 0.58)]), 0.67);
    assert.equal(conf([sig("image.ela", [A], 0.62), sig("image.noise", [A], 0.58), sig("image.copymove", [A], 0.3)]), 0.72);
  });
  it("gives no bonus when the same family agrees with itself", () => {
    assert.equal(conf([sig("image.ela", [A], 0.62), sig("image.ela", [A], 0.58)]), 0.62);
  });
  it("is capped at 1.0", () => {
    assert.equal(conf([sig("image.ela", [A], 0.98), sig("image.noise", [A], 0.5), sig("image.copymove", [A], 0.5)]), 1);
  });
  it("an unknown detector counts as its own family", () => {
    assert.equal(conf([sig("mystery.a", [A], 0.5), sig("mystery.b", [A], 0.5)]), 0.55);
  });
  it("the bonus comes from config", () => {
    assert.equal(conf([sig("image.ela", [A], 0.5), sig("image.noise", [A], 0.5)], { config: { ...DEFAULT_EVENT_CONFIG, agreementBonus: 0.2 } }), 0.7);
  });
});

describe("event regions and shape", () => {
  it("overlapping regions merge into one union box; the label of the first is kept", () => {
    const [e] = group([sig("image.ela", [A], 0.5), sig("image.noise", [shifted(0.05)], 0.5)]);
    assert.deepEqual(e?.regions.map((r) => r.bbox), [[0.1, 0.1, 0.25, 0.1]]);
  });
  it("one signal with two disjoint regions keeps both, in one event", () => {
    const [e] = group([sig("image.ela", [A, [0.6, 0.6, 0.1, 0.1]])]);
    assert.equal(e?.regions.length, 2);
    assert.deepEqual(e?.regions[0]?.bbox, A);
  });
  it("a lone region is copied unchanged and does not alias the input", () => {
    const s = sig("image.ela", [A]);
    const [e] = group([s]);
    assert.deepEqual(e?.regions, s.regions);
    e!.regions[0]!.bbox[0] = 0.99;
    assert.equal(s.regions[0]?.bbox[0], A[0]);
  });
  it("page is the lowest member page; a signal with no region is a document-level event on page 0", () => {
    const [e1] = group([sig("image.ela", [A], 0.5, 3)]);
    assert.equal(e1?.page, 3);
    const doclevel = { ...sig("pdf.metadata", [], 0.4), regions: [] as Region[] };
    const [e2] = group([doclevel]);
    assert.deepEqual([e2?.page, e2?.regions, e2?.detectors], [0, [], ["pdf.metadata"]]);
  });
  it("provenance echoes the thresholds, the document hash and the timestamp", () => {
    const [e] = group([sig("image.ela", [A])], { docSha256: { D: "ab".repeat(32) }, processedAt: "2026-01-01T00:00:00Z" });
    assert.deepEqual(e?.provenance, { analyzer: "eventGrouper", analyzerVersion: "0.1", config: { iouThreshold: 0.3, agreementBonus: 0.05 }, inputSha256: "ab".repeat(32), processedAt: "2026-01-01T00:00:00Z", status: "AVAILABLE" });
  });
  it("categoryOfDetectors", () => {
    assert.equal(categoryOfDetectors(["image.ela", "image.noise"]), "visual");
    assert.equal(categoryOfDetectors(["pdf.textlayer"]), "pdf_structure");
    assert.equal(categoryOfDetectors(["pdf.textlayer", "pdf.fonts"]), "pdf_structure");
    assert.equal(categoryOfDetectors(["image.ela", "pdf.textlayer"]), "visual");
    assert.equal(categoryOfDetectors(["mystery"]), "visual");
  });
});

describe("contract of the function", () => {
  it("empty input and empty documents", () => {
    assert.deepEqual(groupEvents([], "C"), []);
    assert.deepEqual(groupEvents([doc([])], "C"), []);
  });
  it("is deterministic, and never mutates or aliases its input", () => {
    const input = deepFreeze([doc([sig("image.ela", [A], 0.6), sig("image.noise", [shifted(0.02)], 0.5)])]);
    assert.deepStrictEqual(groupEvents(input, "C"), groupEvents(input, "C"));
  });
  it("the result does not depend on how signals are ordered inside an overlapping cluster", () => {
    const s1 = sig("image.ela", [A], 0.62, 1, "a"), s2 = sig("image.noise", [shifted(0.03)], 0.58, 1, "b");
    const x = group([s1, s2])[0], y = group([s2, s1])[0];
    assert.equal(x?.confidence, y?.confidence);
    assert.deepEqual([...(x?.signalIds ?? [])].sort(), [...(y?.signalIds ?? [])].sort());
  });
  it("rejects duplicate signal ids", () => {
    assert.throws(() => group([sig("a", [A], 0.5, 1, "same"), sig("b", [A], 0.5, 1, "same")]), /duplicate signalId/);
  });
  it("every signal lands in exactly one event", () => {
    const sigs = [sig("image.ela", [A]), sig("image.noise", [shifted(0.02)]), sig("image.ela", [[0.7, 0.7, 0.1, 0.1]]), sig("pdf.textlayer", [], 0.4)];
    const ids = group(sigs).flatMap((e) => e.signalIds);
    assert.deepEqual([...ids].sort(), sigs.map((s) => s.signalId).sort());
  });
});

describe("signal ids are unique per document", () => {
  it("the same signal id in two documents is fine (a per-document numbering such as SIG-001)", () => {
    const x = { docId: "D1", signals: [sig("image.ela", [A], 0.5, 1, "SIG-001")] }, y = { docId: "D2", signals: [sig("image.noise", [A], 0.5, 1, "SIG-001")] };
    const ev = groupEvents([x, y], "C");
    assert.deepEqual(ev.map((e) => [e.docId, e.signalIds]), [["D1", ["SIG-001"]], ["D2", ["SIG-001"]]]);
  });
});
