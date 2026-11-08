import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import type { ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, AnalyzerStatus, Finding, Severity } from "../../../contracts/types";
import { DEFAULT_PRIORITY_CONFIG, PriorityConfigError, bandOf, computePriority, effectiveStatuses, validatePriorityConfig } from "./priorityEngine";

const fx = resolve(dirname(fileURLToPath(import.meta.url)), "../../../contracts/fixtures");
const readJson = <T>(p: string): T => JSON.parse(readFileSync(resolve(fx, p), "utf8")) as T;
const CASES: Record<string, string[]> = { "CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"] };

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object") { Object.values(o as object).forEach(deepFreeze); Object.freeze(o); }
  return o;
}

let n = 0;
function F(severity: Severity, confidence: number, category: Finding["category"] = "cross_document", docIds: string[] = ["D1"]): Finding {
  n++;
  return {
    findingId: `F-${String(n).padStart(4, "0")}`, caseId: "C", category, type: "T", observationStatus: "INCONSISTENT", severity, confidence, needsVerification: ["x"],
    evidence: [{
      evidenceId: `E-${n}`, kind: "rule_result", description: "d", regions: docIds.map((docId) => ({ docId, page: 1, bbox: [0.1, 0.1, 0.1, 0.1] as [number, number, number, number] })),
      provenance: { analyzer: "a", analyzerVersion: "0", config: {}, inputSha256: "0".repeat(64), processedAt: "t", status: "AVAILABLE" },
    }],
  };
}
const OK: Record<string, AnalyzerStatus> = { ocr: "AVAILABLE", "image.ela": "AVAILABLE" };
const STAT = { D1: OK };
/** The pure coverage arithmetic, without the expected-analyzer list (that part is tested in "expected analyzers"). */
const NONE = { config: { ...DEFAULT_PRIORITY_CONFIG, expectedAnalyzers: [] as string[] } };

describe("fixtures", () => {
  for (const [caseId, docs] of Object.entries(CASES)) {
    it(`${caseId}: priority equals the fixture priority`, () => {
      const a = docs.map((d) => readJson<AnalyzeResponse>(`analyze/${d}.json`));
      const sha: Record<string, string> = {};
      a.forEach((x) => { sha[x.docId] = x.provenance[0]?.inputSha256 ?? ""; });
      const exp = readJson<ReasoningOutput>(`${caseId}.reasoning.json`);
      const statuses = Object.fromEntries(a.map((x) => [x.docId, x.analyzerStatuses]));
      assert.deepStrictEqual(computePriority({ findings: exp.findings, analyzerStatuses: statuses }, { docSha256: sha, processedAt: "2026-11-01T09:00:00Z" }), exp.priority);
    });
  }
});

describe("spec scenarios", () => {
  it("an analyzer outage lowers coverage but does not lower the band or the score", () => {
    const findings = [F("high", 0.9), F("medium", 0.7, "visual")];
    const healthy = computePriority({ findings, analyzerStatuses: { D1: { ocr: "AVAILABLE", "image.ela": "AVAILABLE", "image.noise": "AVAILABLE", signature: "AVAILABLE" } } }, NONE);
    const outage = computePriority({ findings, analyzerStatuses: { D1: { ocr: "AVAILABLE", "image.ela": "AVAILABLE", "image.noise": "UNAVAILABLE", signature: "ERROR" } } }, NONE);
    assert.equal(healthy.perCase.coverage, 1);
    assert.equal(outage.perCase.coverage, 0.5);
    assert.equal(outage.perCase.priorityBand, healthy.perCase.priorityBand);
    assert.equal(outage.perCase.score, healthy.perCase.score);
    assert.equal(outage.perDocument["D1"]?.coverage, 0.5);
  });
  it("a missing analyzer contributes nothing: no findings and an outage is low priority, with low coverage, not 'clean'", () => {
    const r = computePriority({ findings: [], analyzerStatuses: { D1: { ocr: "AVAILABLE", ela: "UNAVAILABLE", noise: "UNAVAILABLE", fonts: "UNAVAILABLE" } } }, NONE);
    assert.deepEqual([r.perCase.priorityBand, r.perCase.score, r.perCase.confidence, r.perCase.coverage, r.perCase.contributingFindingIds], ["low", 0, 0, 0.25, []]);
  });
  it("NOT_APPLICABLE analyzers are excluded from coverage; all not applicable gives 0", () => {
    assert.equal(computePriority({ findings: [], analyzerStatuses: { D1: { a: "AVAILABLE", b: "NOT_APPLICABLE", c: "NOT_APPLICABLE" } } }, NONE).perCase.coverage, 1);
    assert.equal(computePriority({ findings: [], analyzerStatuses: { D1: { a: "NOT_APPLICABLE" } } }, NONE).perCase.coverage, 0);
  });
});

describe("scoring", () => {
  it("severity weights multiply confidence; the same confidence at higher severity scores higher", () => {
    const score = (s: Severity) => computePriority({ findings: [F(s, 0.8)], analyzerStatuses: STAT }).perCase.score;
    assert.ok(score("info") < score("low") && score("low") < score("medium") && score("medium") < score("high"));
    assert.equal(score("high"), 0.56); // 0.7 * 0.8, category weight 1.0
  });
  it("confidence and severity stay separate: low confidence lowers the score but not the reported severity", () => {
    const hi = computePriority({ findings: [F("high", 0.9)], analyzerStatuses: STAT }).perCase;
    const lo = computePriority({ findings: [F("high", 0.3)], analyzerStatuses: STAT }).perCase;
    assert.ok(lo.score < hi.score);
    assert.equal(lo.confidence, 0.3);
    assert.equal(hi.confidence, 0.9);
  });
  it("noisy-OR within a category: more findings never lower the score", () => {
    let prev = 0;
    const fs: Finding[] = [];
    for (const sev of ["low", "medium", "low", "high", "info"] as Severity[]) {
      fs.push(F(sev, 0.7));
      const s = computePriority({ findings: fs, analyzerStatuses: STAT }).perCase.score;
      assert.ok(s >= prev, `${s} >= ${prev}`);
      prev = s;
    }
  });
  it("categories combine by weight: a cross-document finding counts more than the same finding as extraction", () => {
    const s = (c: Finding["category"]) => computePriority({ findings: [F("high", 1, c)], analyzerStatuses: STAT }).perCase.score;
    assert.equal(s("cross_document"), 0.7);
    assert.equal(s("extraction"), 0.28);
    assert.ok(s("cross_document") > s("context") && s("context") > s("visual") && s("visual") > s("pdf_structure") && s("pdf_structure") > s("extraction"));
  });
  it("evidence from several categories reinforces: two medium findings in two categories beat the stronger single one", () => {
    const two = computePriority({ findings: [F("medium", 0.9, "cross_document"), F("medium", 0.9, "visual")], analyzerStatuses: STAT }).perCase.score;
    const one = computePriority({ findings: [F("medium", 0.9, "cross_document")], analyzerStatuses: STAT }).perCase.score;
    assert.ok(two > one);
  });
  it("categories made only of info/low findings are capped at the moderate band, however many there are", () => {
    const many = Array.from({ length: 40 }, () => F("low", 1, "cross_document"));
    const r = computePriority({ findings: many, analyzerStatuses: STAT }).perCase;
    assert.ok(r.score <= DEFAULT_PRIORITY_CONFIG.weakEvidenceCap);
    assert.equal(r.priorityBand, "moderate");
  });
  it("weak evidence spread over every category still cannot reach the high bands", () => {
    const cats: Finding["category"][] = ["cross_document", "context", "visual", "pdf_structure", "extraction"];
    const fs = cats.flatMap((c) => Array.from({ length: 10 }, () => F("low", 1, c)));
    assert.equal(computePriority({ findings: fs, analyzerStatuses: STAT }).perCase.priorityBand, "moderate");
  });
  it("one medium finding lifts the cap: the same weak findings plus a medium one can score higher", () => {
    const weak = Array.from({ length: 10 }, () => F("low", 1, "cross_document"));
    const base = computePriority({ findings: weak, analyzerStatuses: STAT }).perCase.score;
    const lifted = computePriority({ findings: [...weak, F("medium", 1, "context")], analyzerStatuses: STAT }).perCase.score;
    assert.ok(lifted > base);
  });
  it("confidence is the severity-weighted mean of the contributing findings", () => {
    const r = computePriority({ findings: [F("high", 1), F("low", 0.5)], analyzerStatuses: STAT }).perCase;
    assert.equal(r.confidence, Math.round(((0.7 * 1 + 0.15 * 0.5) / 0.85) * 100) / 100);
  });
});

describe("bands", () => {
  const b = DEFAULT_PRIORITY_CONFIG.bands;
  it("boundaries: low < .25, moderate < .50, high < .75, critical otherwise", () => {
    assert.deepEqual([0, 0.249, 0.25, 0.499, 0.5, 0.749, 0.75, 1].map((s) => bandOf(s, b)), ["low", "low", "moderate", "moderate", "high", "high", "critical", "critical"]);
  });
  it("weights and thresholds come from config", () => {
    const cfg = { ...DEFAULT_PRIORITY_CONFIG, severityWeights: { ...DEFAULT_PRIORITY_CONFIG.severityWeights, high: 0.25 } };
    const r = computePriority({ findings: [F("high", 1)], analyzerStatuses: STAT }, { config: cfg }).perCase;
    assert.deepEqual([r.score, r.priorityBand], [0.25, "moderate"]);
    const strict = { ...DEFAULT_PRIORITY_CONFIG, bands: { lowBelow: 0.1, moderateBelow: 0.2, highBelow: 0.3 } };
    assert.equal(computePriority({ findings: [F("high", 1)], analyzerStatuses: STAT }, { config: strict }).perCase.priorityBand, "critical");
  });
  it("every finding category has a weight in the shipped config", () => {
    for (const c of ["extraction", "visual", "pdf_structure", "context", "cross_document"]) assert.ok((DEFAULT_PRIORITY_CONFIG.categoryWeights as Record<string, number>)[c] !== undefined, c);
  });
});

describe("per document and per case", () => {
  it("a finding counts for every document its evidence touches; documents without findings are low", () => {
    const f1 = F("high", 0.9, "cross_document", ["D1", "D2"]), f2 = F("medium", 0.8, "visual", ["D3"]);
    const r = computePriority({ findings: [f1, f2], analyzerStatuses: { D1: OK, D2: OK, D3: OK, D4: OK } });
    assert.deepEqual(r.perDocument["D1"]?.contributingFindingIds, [f1.findingId]);
    assert.deepEqual(r.perDocument["D2"]?.contributingFindingIds, [f1.findingId]);
    assert.deepEqual(r.perDocument["D3"]?.contributingFindingIds, [f2.findingId]);
    assert.deepEqual([r.perDocument["D4"]?.priorityBand, r.perDocument["D4"]?.score], ["low", 0]);
    assert.deepEqual(r.perCase.contributingFindingIds, [f1.findingId, f2.findingId]);
  });
  it("case coverage pools the analyzers of all documents", () => {
    const r = computePriority({ findings: [], analyzerStatuses: { D1: { a: "AVAILABLE", b: "AVAILABLE" }, D2: { a: "AVAILABLE", b: "UNAVAILABLE" } } }, NONE);
    assert.equal(r.perCase.coverage, 0.75);
    assert.equal(r.perDocument["D1"]?.coverage, 1);
    assert.equal(r.perDocument["D2"]?.coverage, 0.5);
  });
});

describe("contract of the function", () => {
  it("echoes every weight and threshold into provenance", () => {
    const p = computePriority({ findings: [], analyzerStatuses: STAT }, { processedAt: "2026-01-01T00:00:00Z" }).provenance;
    assert.equal(p.analyzer, "priorityEngine");
    assert.deepEqual(p.config, {
      severityWeight_info: 0.05, severityWeight_low: 0.15, severityWeight_medium: 0.4, severityWeight_high: 0.7,
      categoryWeight_cross_document: 1, categoryWeight_context: 0.9, categoryWeight_visual: 0.8, categoryWeight_pdf_structure: 0.7, categoryWeight_extraction: 0.4,
      bandLowBelow: 0.25, bandModerateBelow: 0.5, bandHighBelow: 0.75, weakEvidenceCap: 0.49,
      lowCoverageBelow: 0.75, expectedAnalyzers: DEFAULT_PRIORITY_CONFIG.expectedAnalyzers.join(","),
    });
    assert.equal(p.processedAt, "2026-01-01T00:00:00Z");
  });
  it("calls the result priority, never risk or fraud", () => {
    const out = computePriority({ findings: [F("high", 0.9)], analyzerStatuses: STAT });
    const blob = JSON.stringify(out).toLowerCase();
    assert.ok(!blob.includes("risk") && !blob.includes("fraud"));
    assert.ok("perCase" in out && "priorityBand" in out.perCase);
  });
  it("is deterministic and never mutates its input", () => {
    const input = deepFreeze({ findings: [F("high", 0.9), F("low", 0.5, "context")], analyzerStatuses: { D1: OK } });
    assert.deepStrictEqual(computePriority(input), computePriority(input));
  });
  it("empty case", () => {
    const r = computePriority({ findings: [], analyzerStatuses: {} });
    assert.deepEqual([r.perCase.priorityBand, r.perCase.score, r.perCase.coverage, r.perDocument], ["low", 0, 0, {}]);
  });
  it("scores stay within 0..1 whatever the input", () => {
    for (let k = 0; k < 50; k++) {
      const fs = Array.from({ length: k }, (_, i) => F((["info", "low", "medium", "high"] as Severity[])[i % 4] ?? "low", ((i * 37) % 100) / 100, (["cross_document", "context", "visual", "pdf_structure", "extraction"] as Finding["category"][])[i % 5]));
      const s = computePriority({ findings: fs, analyzerStatuses: STAT }).perCase.score;
      assert.ok(s >= 0 && s <= 1);
    }
  });
});

describe("expected analyzers (a missing check is never clean)", () => {
  const conflict = [F("high", 0.9)];
  const reported = (o: Record<string, AnalyzerStatus>) => ({ D1: o });

  it("a case with a conflict and only OCR available is not reported as fully covered", () => {
    const r = computePriority({ findings: conflict, analyzerStatuses: reported({ ocr: "AVAILABLE" }) });
    assert.ok(r.perCase.coverage < 0.5, String(r.perCase.coverage));
    assert.equal(r.perCase.incompleteCoverage, true);
    assert.ok(r.perCase.missingAnalyzers?.includes("image.copymove") && !r.perCase.missingAnalyzers.includes("ocr"));
    assert.deepEqual(r.perDocument["D1"]?.missingAnalyzers, r.perCase.missingAnalyzers);
  });
  it("the same findings, nothing else changed: coverage drops but score and band do not move", () => {
    const full = computePriority({ findings: conflict, analyzerStatuses: reported(Object.fromEntries(DEFAULT_PRIORITY_CONFIG.expectedAnalyzers.map((id) => [id, "AVAILABLE" as const]))) });
    const ocrOnly = computePriority({ findings: conflict, analyzerStatuses: reported({ ocr: "AVAILABLE" }) });
    assert.equal(full.perCase.coverage, 1);
    assert.equal(full.perCase.incompleteCoverage, undefined);
    assert.equal(full.perCase.missingAnalyzers, undefined);
    assert.equal(ocrOnly.perCase.score, full.perCase.score);
    assert.equal(ocrOnly.perCase.priorityBand, full.perCase.priorityBand);
  });
  it("no findings and no analyzers reported: low band, coverage 0 and flagged, not clean", () => {
    const r = computePriority({ findings: [], analyzerStatuses: { D1: {} } });
    assert.deepEqual([r.perCase.priorityBand, r.perCase.coverage, r.perCase.incompleteCoverage], ["low", 0, true]);
    assert.deepEqual(r.perCase.missingAnalyzers, [...DEFAULT_PRIORITY_CONFIG.expectedAnalyzers].sort());
  });
  it("NOT_APPLICABLE is not missing, and a family that reports only NOT_APPLICABLE does not apply to the document", () => {
    const image = effectiveStatuses({ ocr: "AVAILABLE", "pdf.text_layer_words": "NOT_APPLICABLE", "image.ela": "AVAILABLE" }, DEFAULT_PRIORITY_CONFIG.expectedAnalyzers);
    assert.ok(!image.missing.includes("pdf.fonts") && !image.missing.includes("pdf.metadata") && !image.missing.includes("pdf.revisions"));
    assert.ok(image.missing.includes("image.noise") && image.missing.includes("signature"));
    // a PDF that reports some pdf.* analyzers: the others it should have run are missing
    const pdf = effectiveStatuses({ ocr: "AVAILABLE", "pdf.text_layer_words": "AVAILABLE" }, DEFAULT_PRIORITY_CONFIG.expectedAnalyzers);
    assert.ok(pdf.missing.includes("pdf.fonts") && pdf.missing.includes("pdf.metadata"));
  });
  it("a family with no reported member is missing (an analyzer that was never built is not 'not applicable')", () => {
    const s = effectiveStatuses({ ocr: "AVAILABLE" }, ["ocr", "pdf.fonts"]);
    assert.deepEqual(s.missing, ["pdf.fonts"]);
  });
  it("reported UNAVAILABLE or ERROR expected analyzers are listed as missing; unexpected extra analyzers still count", () => {
    const s = effectiveStatuses({ ocr: "AVAILABLE", signature: "UNAVAILABLE", "image.ela": "ERROR", extra: "AVAILABLE" }, ["ocr", "signature", "image.ela"]);
    assert.deepEqual(s.missing.sort(), ["image.ela", "signature"]);
    assert.equal(s.statuses.filter((x) => x === "AVAILABLE").length, 2);
  });
  it("the threshold is exclusive: coverage equal to lowCoverageBelow is not flagged", () => {
    const cfg = { ...DEFAULT_PRIORITY_CONFIG, expectedAnalyzers: ["a", "b"], lowCoverageBelow: 0.5 };
    assert.equal(computePriority({ findings: [], analyzerStatuses: reported({ a: "AVAILABLE" }) }, { config: cfg }).perCase.incompleteCoverage, undefined);
    assert.equal(computePriority({ findings: [], analyzerStatuses: reported({}) }, { config: cfg }).perCase.incompleteCoverage, true);
  });
  it("case-level missing analyzers are the sorted union over documents", () => {
    const cfg = { ...DEFAULT_PRIORITY_CONFIG, expectedAnalyzers: ["a", "b", "c"] };
    const r = computePriority({ findings: [], analyzerStatuses: { D1: { a: "AVAILABLE", b: "AVAILABLE" }, D2: { a: "AVAILABLE", c: "AVAILABLE" } } }, { config: cfg });
    assert.deepEqual(r.perCase.missingAnalyzers, ["b", "c"]);
    assert.deepEqual(r.perDocument["D1"]?.missingAnalyzers, ["c"]);
  });
  it("does not mutate its input and is deterministic", () => {
    const input = deepFreeze({ findings: conflict, analyzerStatuses: reported({ ocr: "AVAILABLE" }) });
    assert.deepStrictEqual(computePriority(input), computePriority(input));
  });
});

describe("expected analyzers: configuration is validated", () => {
  const bad = (patch: Record<string, unknown>) => () => validatePriorityConfig({ ...DEFAULT_PRIORITY_CONFIG, ...patch } as never);
  it("accepts the shipped configuration", () => assert.doesNotThrow(() => validatePriorityConfig(DEFAULT_PRIORITY_CONFIG)));
  for (const [label, patch] of [
    ["an empty list", { expectedAnalyzers: [] }],
    ["a missing list", { expectedAnalyzers: undefined }],
    ["a non-list", { expectedAnalyzers: "ocr" }],
    ["an empty id", { expectedAnalyzers: ["ocr", ""] }],
    ["a padded id", { expectedAnalyzers: ["ocr "] }],
    ["a non-string id", { expectedAnalyzers: ["ocr", 3] }],
    ["duplicates", { expectedAnalyzers: ["ocr", "ocr"] }],
    ["a zero threshold", { lowCoverageBelow: 0 }],
    ["a threshold above 1", { lowCoverageBelow: 1.2 }],
    ["a NaN threshold", { lowCoverageBelow: Number.NaN }],
    ["a missing threshold", { lowCoverageBelow: undefined }],
  ] as const) {
    it(`rejects ${label}`, () => assert.throws(bad(patch), PriorityConfigError));
  }
});
