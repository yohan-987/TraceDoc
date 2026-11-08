import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import type { ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, AnalyzerStatus, Finding, ForensicSignal, Region } from "../../../contracts/types";
import { runReasoning } from "./index";
import { severityOf } from "./eventFindings";

const here = dirname(fileURLToPath(import.meta.url));
const fx = resolve(here, "../../../contracts/fixtures");
const readJson = <T>(p: string): T => JSON.parse(readFileSync(resolve(fx, p), "utf8")) as T;
const CASES: Record<string, string[]> = { "CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"] };
const load = (caseId: string): AnalyzeResponse[] => (CASES[caseId] ?? []).map((d) => readJson<AnalyzeResponse>(`analyze/${d}.json`));
const clone = <T>(o: T): T => structuredClone(o);

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object") { Object.values(o as object).forEach(deepFreeze); Object.freeze(o); }
  return o;
}
const withStatus = (docs: AnalyzeResponse[], docId: string, analyzer: string, status: AnalyzerStatus): AnalyzeResponse[] =>
  docs.map((d) => (d.docId === docId ? { ...d, analyzerStatuses: { ...d.analyzerStatuses, [analyzer]: status } } : d));
const run = (caseId: string, docs = load(caseId)) => runReasoning({ caseId, documents: docs });

describe("fixtures: the facade reproduces the recorded reasoning output", () => {
  for (const caseId of Object.keys(CASES)) {
    it(`${caseId}: output equals contracts/fixtures/${caseId}.reasoning.json exactly`, () => {
      assert.deepStrictEqual(run(caseId), readJson<ReasoningOutput>(`${caseId}.reasoning.json`));
    });
    it(`${caseId}: every expected finding appears once and every expected non-finding is absent`, () => {
      const out = run(caseId);
      const expF = readJson<Record<string, any[]>>("expected_findings.json")[caseId] ?? [];
      const expN = readJson<Record<string, any[]>>("expected_non_findings.json")[caseId] ?? [];
      for (const exp of expF) {
        const hit = out.findings.filter((f) => {
          if (f.type !== exp.type) return false;
          if (exp.assertionIds) return exp.assertionIds.every((id: string) => f.evidence.some((e) => e.assertionIds?.includes(id)));
          const ev = out.events.find((e) => exp.signalIds.every((s: string) => e.signalIds.includes(s)));
          return !!ev && f.evidence.some((e) => e.measurements?.["eventId"] === ev.eventId);
        });
        assert.equal(hit.length, 1, `${exp.expectedId} ${exp.type}`);
        assert.equal(hit[0]?.severity, exp.severity);
        assert.equal(hit[0]?.category, exp.category);
        assert.equal(hit[0]?.observationStatus, exp.observationStatus);
        if (exp.maxConfidence !== undefined) assert.ok((hit[0]?.confidence ?? 1) <= exp.maxConfidence);
      }
      assert.equal(out.findings.length, expF.length, "no finding beyond the expected ones");
      for (const nf of expN) {
        const bad = out.findings.filter((f) => nf.assertionIds.every((id: string) => f.evidence.some((e) => e.assertionIds?.includes(id))));
        assert.deepEqual(bad.map((f) => f.type), [], `${nf.nonFindingId}: ${nf.description}`);
      }
    });
  }
  it("CASE-001: ELA and noise on the amount region are ONE finding, not two", () => {
    const out = run("CASE-001");
    assert.equal(out.findings.filter((f) => f.type === "REGION_EVENT").length, 1);
    assert.equal(out.findings.filter((f) => f.type === "ELA_ANOMALY" || f.type === "NOISE_INCONSISTENCY").length, 0);
  });
  it("CASE-002: applicant and guarantor stay separate and nothing is found", () => {
    const out = run("CASE-002");
    assert.equal(out.entities.length, 2);
    assert.deepEqual(out.findings, []);
    assert.equal(out.priority.perCase.priorityBand, "low");
  });
});

describe("honesty: analyzer statuses", () => {
  it("a signal from an ERROR analyzer is ignored, so the event shrinks to the remaining detector", () => {
    const out = run("CASE-001", withStatus(load("CASE-001"), "D-02", "image.noise", "ERROR"));
    const f = out.findings.find((x) => x.evidence.some((e) => e.measurements?.["signalIds"] === "D-02-S01"));
    assert.equal(f?.type, "ELA_ANOMALY");
    assert.equal(f?.confidence, 0.62);
    assert.equal(out.findings.filter((x) => x.type === "REGION_EVENT").length, 0);
  });
  it("UNAVAILABLE and NOT_APPLICABLE analyzers are ignored too, and the document shows reduced coverage", () => {
    for (const status of ["UNAVAILABLE", "NOT_APPLICABLE"] as const) {
      const out = run("CASE-001", withStatus(load("CASE-001"), "D-03", "pdf.textlayer", status));
      assert.equal(out.findings.some((f) => f.type === "TEXT_LAYER_MISMATCH"), false, status);
    }
    const out = run("CASE-001", withStatus(load("CASE-001"), "D-03", "pdf.textlayer", "UNAVAILABLE"));
    assert.ok(out.priority.perDocument["D-03"]!.coverage < 1);
  });
  it("an outage in an analyzer that found nothing lowers coverage but not the score or the band", () => {
    const base = run("CASE-001");
    const out = run("CASE-001", withStatus(load("CASE-001"), "D-01", "image.copymove", "UNAVAILABLE"));
    assert.ok(out.priority.perCase.coverage < base.priority.perCase.coverage);
    assert.equal(out.priority.perCase.score, base.priority.perCase.score);
    assert.equal(out.priority.perCase.priorityBand, base.priority.perCase.priorityBand);
    assert.deepEqual(out.findings, base.findings);
  });
  it("a missing analyzer on a clean case is low priority with reduced coverage, never reported as fully checked", () => {
    const out = run("CASE-002", withStatus(load("CASE-002"), "D-11", "image.ela", "ERROR"));
    assert.equal(out.priority.perCase.priorityBand, "low");
    assert.ok(out.priority.perCase.coverage < 1);
    assert.deepEqual(out.findings, []);
  });
  it("a signal from a detector with no recorded status is kept", () => {
    const docs = load("CASE-001");
    const d2 = docs.find((d) => d.docId === "D-02")!;
    delete (d2.analyzerStatuses as Record<string, AnalyzerStatus>)["image.ela"];
    delete (d2.analyzerStatuses as Record<string, AnalyzerStatus>)["image.noise"];
    assert.equal(run("CASE-001", docs).findings.filter((f) => f.type === "REGION_EVENT").length, 1);
  });
});

describe("shape and numbering", () => {
  it("rule findings come first, then events; ids are continuous and unique", () => {
    const out = run("CASE-001");
    assert.deepEqual(out.findings.map((f) => f.findingId), ["F-0001", "F-0002", "F-0003", "F-0004", "F-0005", "F-0006"]);
    assert.deepEqual(out.findings.slice(4).map((f) => f.category), ["visual", "pdf_structure"]);
    const ev = out.findings.flatMap((f) => f.evidence.map((e) => e.evidenceId));
    assert.equal(new Set(ev).size, ev.length);
  });
  it("event findings are POTENTIAL_ANOMALY, link the underlying field, and keep severity apart from confidence", () => {
    const out = run("CASE-001");
    for (const f of out.findings.filter((x) => x.category === "visual" || x.category === "pdf_structure")) {
      assert.equal(f.observationStatus, "POTENTIAL_ANOMALY");
      assert.ok(f.evidence[0]?.assertionIds?.length);
      assert.ok(f.needsVerification.length > 0);
    }
  });
  it("severity thresholds come from config: below 0.4 low, below 0.75 medium, else high", () => {
    assert.deepEqual([0, 0.39, 0.4, 0.74, 0.75, 1].map((c) => severityOf(c)), ["low", "low", "medium", "medium", "high", "high"]);
  });
  it("priority covers every document, including documents with no findings", () => {
    assert.deepEqual(Object.keys(run("CASE-001").priority.perDocument).sort(), ["D-01", "D-02", "D-03"]);
  });
  it("findings never use language that implies intent or a verdict", () => {
    const blob = JSON.stringify([run("CASE-001").findings, run("CASE-002").findings]).toLowerCase();
    for (const bad of ["forged", "fraud", "intentional", "deliberate", "fake", "tamper", "risk"]) assert.ok(!blob.includes(bad), bad);
  });
});

describe("event findings in detail", () => {
  const baseDoc = (signals: ForensicSignal[], statuses: Record<string, AnalyzerStatus> = {}): AnalyzeResponse => ({
    docId: "X", pipelineVersion: "t", pages: [{ docId: "X", page: 1, renderPath: "x.png", renderDpi: 150, rotationDeg: 0, deskewDeg: 0, docType: "other", docTypeConfidence: 0.9 }],
    fields: [], signals, analyzerStatuses: statuses, provenance: [{ analyzer: "ocr", analyzerVersion: "0", config: {}, inputSha256: "f".repeat(64), processedAt: "2026-05-05T00:00:00Z", status: "AVAILABLE" }],
  });
  const sig = (id: string, detector: string, type: string, regions: Region[], confidence: number): ForensicSignal => ({
    signalId: id, detector, type, regions, confidence, measurements: { k: 1 },
    provenance: { analyzer: detector, analyzerVersion: "0", config: {}, inputSha256: "f".repeat(64), processedAt: "2026-05-05T00:00:00Z", status: "AVAILABLE" },
  });
  const R = (bbox: Region["bbox"], label?: string): Region => ({ docId: "X", page: 1, bbox, ...(label ? { label } : {}) });

  it("a document-level signal becomes a pdf_structure finding with a whole-page region, attributed to its document", () => {
    const out = runReasoning({ caseId: "C", documents: [baseDoc([sig("S1", "pdf.metadata", "PDF_METADATA", [], 0.4)], { "pdf.metadata": "AVAILABLE" })] });
    const f = out.findings[0]!;
    assert.deepEqual([f.type, f.category, f.severity, f.evidence[0]?.kind], ["PDF_METADATA", "pdf_structure", "medium", "metadata"]);
    assert.deepEqual(f.evidence[0]?.regions, [{ docId: "X", page: 1, bbox: [0, 0, 1, 1], label: "document" }]);
    assert.equal(out.events[0]?.page, 0);
    assert.deepEqual(out.priority.perDocument["X"]?.contributingFindingIds, [f.findingId]);
  });
  it("the finding lists every field mostly covered by the flagged region", () => {
    const field = (n: number, bbox: Region["bbox"]) => ({
      fieldId: `X-F0${n}`, docId: "X", name: "full_name", rawValue: "a", normalizedValue: "a", ocrConfidence: 0.9, extractionConfidence: 0.9,
      validationStatus: "NOT_CHECKED" as const, region: R(bbox), source: "ocr" as const,
    });
    const doc = { ...baseDoc([sig("S1", "image.ela", "ELA_ANOMALY", [R([0.1, 0.1, 0.5, 0.3])], 0.6)], { "image.ela": "AVAILABLE" }),
      fields: [field(1, [0.12, 0.12, 0.1, 0.05]), field(2, [0.12, 0.25, 0.1, 0.05]), field(3, [0.8, 0.8, 0.1, 0.05])] };
    const out = runReasoning({ caseId: "C", documents: [doc] });
    assert.deepEqual(out.findings[0]?.evidence[0]?.assertionIds, ["X-A01", "X-A02"]);
  });
  it("a flagged region that covers no field simply has no assertion link", () => {
    const out = runReasoning({ caseId: "C", documents: [baseDoc([sig("S1", "image.ela", "ELA_ANOMALY", [R([0.5, 0.5, 0.1, 0.1])], 0.6)], { "image.ela": "AVAILABLE" })] });
    assert.equal(out.findings[0]?.evidence[0]?.assertionIds, undefined);
  });
  it("unknown signal types still produce a valid, honest finding", () => {
    const out = runReasoning({ caseId: "C", documents: [baseDoc([sig("S1", "image.ela", "SOMETHING_NEW", [R([0.5, 0.5, 0.1, 0.1])], 0.9)], { "image.ela": "AVAILABLE" })] });
    const f = out.findings[0]!;
    assert.deepEqual([f.type, f.severity, f.observationStatus], ["SOMETHING_NEW", "high", "POTENTIAL_ANOMALY"]);
    assert.ok(f.needsVerification.length > 0);
  });
  it("provenance timestamps and hashes come from the documents, not from a clock", () => {
    const out = runReasoning({ caseId: "C", documents: [baseDoc([sig("S1", "image.ela", "ELA_ANOMALY", [R([0.5, 0.5, 0.1, 0.1])], 0.6)], { "image.ela": "AVAILABLE" })] });
    assert.equal(out.events[0]?.provenance.processedAt, "2026-05-05T00:00:00Z");
    assert.equal(out.events[0]?.provenance.inputSha256, "f".repeat(64));
    assert.equal(out.priority.provenance.processedAt, "2026-05-05T00:00:00Z");
  });
});

describe("contract of the function", () => {
  it("is deterministic: the same input gives the same output", () => {
    const docs = load("CASE-001");
    assert.deepStrictEqual(runReasoning({ caseId: "CASE-001", documents: docs }), runReasoning({ caseId: "CASE-001", documents: clone(docs) }));
  });
  it("never mutates its input", () => {
    const docs = deepFreeze(load("CASE-001"));
    assert.doesNotThrow(() => runReasoning({ caseId: "CASE-001", documents: docs }));
  });
  it("does not depend on the order of documents for what is found", () => {
    const docs = load("CASE-001");
    const a = runReasoning({ caseId: "CASE-001", documents: docs }), b = runReasoning({ caseId: "CASE-001", documents: [...docs].reverse() });
    const key = (fs: Finding[]) => fs.map((f) => `${f.type}|${f.severity}|${f.confidence}`).sort();
    assert.deepEqual(key(a.findings), key(b.findings));
    assert.equal(a.priority.perCase.score, b.priority.perCase.score);
  });
  it("no documents: an empty, valid output", () => {
    const out = runReasoning({ caseId: "C", documents: [] });
    assert.deepEqual([out.assertions, out.entities, out.findings, out.events], [[], [], [], []]);
    assert.deepEqual([out.priority.perCase.priorityBand, out.priority.perDocument], ["low", {}]);
  });
  it("a document with no fields and no signals is fine", () => {
    const out = runReasoning({ caseId: "C", documents: [load("CASE-001")[0]!].map((d) => ({ ...d, fields: [], signals: [] })) });
    assert.deepEqual([out.assertions, out.findings], [[], []]);
    assert.equal(out.priority.perDocument["D-01"]?.priorityBand, "low");
  });
  it("rejects duplicate document ids", () => {
    const d = load("CASE-001")[0]!;
    assert.throws(() => runReasoning({ caseId: "C", documents: [d, d] }), /duplicate document/);
  });
  it("uses the caseId it is given on everything it produces", () => {
    const out = runReasoning({ caseId: "CASE-XYZ", documents: load("CASE-001") });
    for (const x of [...out.assertions, ...out.entities, ...out.findings, ...out.events]) assert.equal(x.caseId, "CASE-XYZ");
  });
  it("the engine source has no I/O, no clock and no randomness", () => {
    const banned = /node:fs|node:http|node:net|node:child_process|\bfetch\(|process\.env|Date\.now|new Date\(\)|Math\.random|performance\.now/;
    for (const f of readdirSync(here).filter((n) => n.endsWith(".ts") && !n.endsWith(".test.ts"))) {
      const src = readFileSync(resolve(here, f), "utf8").split("\n").filter((l) => !l.trim().startsWith("*") && !l.trim().startsWith("//")).join("\n");
      assert.ok(!banned.test(src), `${f} uses something that is not pure`);
    }
  });
});

describe("per-document ids, as the real Python service emits them (regression: reasoning crashed on real output)", () => {
  /** Relabels a fixture the way the service does: FLD-001... and SIG-001... numbered inside each document. */
  const perDocumentIds = (docs: AnalyzeResponse[]): AnalyzeResponse[] =>
    docs.map((d) => ({
      ...d,
      fields: d.fields.map((f, i) => ({ ...f, fieldId: `FLD-${String(i + 1).padStart(3, "0")}` })),
      signals: d.signals.map((s, i) => ({ ...s, signalId: `SIG-${String(i + 1).padStart(3, "0")}` })),
    }));
  const project = (o: ReasoningOutput) => ({
    findings: o.findings.map((f) => [f.type, f.category, f.severity, f.confidence, f.observationStatus]),
    entities: o.entities.map((e) => [e.entityId, e.role, e.resolution, e.assertionIds.length]),
    priority: o.priority.perCase, perDocument: o.priority.perDocument,
  });
  for (const caseId of Object.keys(CASES)) {
    it(`${caseId}: gives the same findings, entities and priority as with fixture-style ids`, () => {
      const real = runReasoning({ caseId, documents: perDocumentIds(load(caseId)) });
      const fixture = run(caseId);
      const stripPriorityIds = (p: any) => ({ ...p, contributingFindingIds: undefined });
      assert.deepStrictEqual(project(real).findings, project(fixture).findings);
      assert.deepStrictEqual(project(real).entities, project(fixture).entities);
      assert.deepStrictEqual(stripPriorityIds(project(real).priority), stripPriorityIds(project(fixture).priority));
    });
    it(`${caseId}: assertion ids are unique and every evidence link points at a real assertion`, () => {
      const out = runReasoning({ caseId, documents: perDocumentIds(load(caseId)) });
      const ids = out.assertions.map((a) => a.assertionId);
      assert.equal(new Set(ids).size, ids.length);
      for (const f of out.findings) for (const e of f.evidence) for (const id of e.assertionIds ?? []) assert.ok(ids.includes(id), id);
    });
  }
  it("CASE-001: the event findings still find the field under the flagged region with per-document signal ids", () => {
    const out = runReasoning({ caseId: "CASE-001", documents: perDocumentIds(load("CASE-001")) });
    const ev = out.findings.filter((f) => f.type === "REGION_EVENT" || f.type === "TEXT_LAYER_MISMATCH");
    assert.equal(ev.length, 2);
    assert.ok(ev.every((f) => (f.evidence[0]?.assertionIds ?? []).length === 1));
    assert.deepEqual(out.events.map((e) => e.signalIds), [["SIG-001", "SIG-002"], ["SIG-001"]]);
  });
});
