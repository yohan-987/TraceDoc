import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import type { ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, ComparabilityRule, DocumentAssertion, Entity, EntityRole, Finding } from "../../../contracts/types";
import { buildAssertions } from "./assertions";
import { evaluate, nextIdStart } from "./comparability";
import { resolveEntities } from "./entityResolver";
import { DEFAULT_RULES } from "./ruleSupport";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const fx = resolve(root, "contracts/fixtures");
const readJson = <T>(p: string): T => JSON.parse(readFileSync(p, "utf8")) as T;
const CASES: Record<string, string[]> = { "CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"] };

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object") { Object.values(o as object).forEach(deepFreeze); Object.freeze(o); }
  return o;
}

function runCase(caseId: string) {
  const a = (CASES[caseId] ?? []).map((d) => readJson<AnalyzeResponse>(`${fx}/analyze/${d}.json`));
  const sha: Record<string, string> = {};
  a.forEach((x) => { sha[x.docId] = x.provenance[0]?.inputSha256 ?? ""; });
  const r = resolveEntities(buildAssertions(a.flatMap((x) => x.fields), a.flatMap((x) => x.pages), caseId));
  const findings = evaluate(r.assertions, r.entities, { docSha256: sha, processedAt: "2026-11-01T09:00:00Z" });
  return { ...r, findings };
}

// ------------------------------------------------------------------------------------------------ hand-built data
let seq = 0;
function A(docId: string, semanticType: string, value: string | null, entityId: string, conf = 0.9): DocumentAssertion {
  seq++;
  return {
    assertionId: `${docId}-A${String(seq).padStart(3, "0")}`, caseId: "C", docId, fieldId: `${docId}-F${seq}`, entityId, entityRole: "applicant",
    semanticType, normalizedValue: value, extractionConfidence: conf, region: { docId, page: 1, bbox: [0.1, 0.1, 0.2, 0.05] },
  };
}
function E(entityId: string, role: EntityRole, as: DocumentAssertion[], resolution: Entity["resolution"] = "RESOLVED", links?: string[]): Entity {
  const e: Entity = { entityId, caseId: "C", role, resolution, basis: [], assertionIds: as.filter((a) => a.entityId === entityId).map((a) => a.assertionId) };
  if (links) e.suggestedLinks = links.map((id) => ({ entityId: id, score: 100, method: "name_token_sort" as const }));
  return e;
}
const one = (as: DocumentAssertion[], ents?: Entity[], opts = {}): Finding[] => {
  const ids = [...new Set(as.map((a) => a.entityId ?? ""))];
  return evaluate(as, ents ?? ids.map((id) => E(id, "applicant", as)), opts);
};
const types = (fs: Finding[]): string[] => fs.map((f) => f.type);

// ------------------------------------------------------------------------------------------------ fixtures
describe("fixtures", () => {
  for (const caseId of Object.keys(CASES)) {
    it(`${caseId}: reproduces the fixture findings that rules (not forensic events) produce`, () => {
      const { findings } = runCase(caseId);
      const expected = readJson<ReasoningOutput>(`${fx}/${caseId}.reasoning.json`).findings.filter((f) => f.category !== "visual" && f.category !== "pdf_structure");
      assert.deepStrictEqual(findings, expected);
    });
    it(`${caseId}: every expected finding appears and every expected non-finding is absent`, () => {
      const { findings } = runCase(caseId);
      const expF = readJson<Record<string, any[]>>(`${fx}/expected_findings.json`)[caseId] ?? [];
      const expN = readJson<Record<string, any[]>>(`${fx}/expected_non_findings.json`)[caseId] ?? [];
      for (const exp of expF.filter((x) => x.assertionIds)) {
        const hit = findings.filter((f) => f.type === exp.type && exp.assertionIds.every((id: string) => f.evidence.some((e) => e.assertionIds?.includes(id))));
        assert.equal(hit.length, 1, `${exp.expectedId} ${exp.type}`);
        const f = hit[0]!;
        assert.equal(f.severity, exp.severity); assert.equal(f.category, exp.category); assert.equal(f.observationStatus, exp.observationStatus);
        if (exp.maxConfidence !== undefined) assert.ok(f.confidence <= exp.maxConfidence, `${exp.expectedId} capped`);
        assert.equal(f.evidence[0]?.measurements?.["ruleId"], exp.ruleId);
      }
      for (const nf of expN) {
        const bad = findings.filter((f) => nf.assertionIds.every((id: string) => f.evidence.some((e) => e.assertionIds?.includes(id))));
        assert.deepEqual(bad.map((f) => f.type), [], `${nf.nonFindingId}: ${nf.description}`);
      }
    });
  }
  it("findings never use language that implies intent or a verdict", () => {
    for (const caseId of Object.keys(CASES)) {
      const blob = JSON.stringify(runCase(caseId).findings).toLowerCase();
      for (const bad of ["forged", "fraud", "intentional", "deliberate", "fake", "tamper"]) assert.ok(!blob.includes(bad), bad);
    }
  });
  it("every finding records its rule id and why legitimate-difference rules did not apply", () => {
    for (const f of runCase("CASE-001").findings.filter((x) => x.evidence[0]?.kind === "assertion_pair" && x.category === "cross_document")) {
      assert.ok(f.evidence[0]?.measurements?.["ruleId"]);
      assert.match(String(f.evidence[0]?.measurements?.["legitimateDifferenceRules"]), /^none apply to/);
    }
  });
});

describe("rule table", () => {
  it("has unique ids and complete rows (the JSON Schema check lives in contracts/validate.ts)", () => {
    assert.equal(new Set(DEFAULT_RULES.map((r) => r.ruleId)).size, DEFAULT_RULES.length);
    for (const r of DEFAULT_RULES) {
      assert.ok(r.ruleId && r.semanticTypes.length > 0 && r.entityRoles.length > 0 && r.note, r.ruleId);
      if (r.ruleType === "MUST_AGREE" || r.ruleType === "ARITHMETIC" || r.ruleType === "TEMPORAL" || r.ruleType === "UNIQUENESS") assert.ok(r.findingType, `${r.ruleId} needs a findingType`);
    }
  });
  it("contains every rule type and every rule id the fixtures refer to", () => {
    assert.deepEqual([...new Set(DEFAULT_RULES.map((r) => r.ruleType))].sort(), ["ARITHMETIC", "MAY_DIFFER", "MUST_AGREE", "NOT_COMPARABLE", "TEMPORAL", "UNIQUENESS"]);
    const ids = new Set(DEFAULT_RULES.map((r) => r.ruleId));
    const used = JSON.stringify([readJson(`${fx}/expected_findings.json`), readJson(`${fx}/expected_non_findings.json`)]);
    for (const m of used.matchAll(/"ruleId": "(R-[A-Z-]+)"/g)) assert.ok(ids.has(m[1] ?? ""), `rule ${m[1]} missing from config`);
  });
});

// ------------------------------------------------------------------------------------------------ MUST_AGREE
describe("name rule bands", () => {
  const names = (a: string, b: string) => one([A("D1", "full_name", a, "E1"), A("D2", "full_name", b, "E1")]);
  it("at or above 92 agrees: no finding", () => assert.deepEqual(names("Aarav Kumar Sharma", "Aarav Kumar Sharmaa"), []));
  it("75 to 92 is a low-severity variant spelling", () => {
    const f = names("Aarav Kumar Sharma", "Aarav Sharma");
    assert.deepEqual(types(f), ["NAME_VARIANT_SPELLING"]);
    assert.equal(f[0]?.severity, "low");
    assert.equal(f[0]?.observationStatus, "INCONSISTENT");
  });
  it("below 75 is a conflict at the rule's severity", () => {
    const f = names("Aarav Kumar Sharma", "Ravi Verma");
    assert.deepEqual(types(f), ["NAME_CONFLICT"]);
    assert.equal(f[0]?.severity, "high");
  });
  it("thresholds come from config, not code", () => {
    const as = [A("D1", "full_name", "Aarav Kumar Sharma", "E1"), A("D2", "full_name", "Aarav Sharma", "E1")]; // ratio 80
    assert.deepEqual(types(one(as, undefined, { comparability: { nameMatchMin: 70, nameVariantMin: 60, unresolvedConfidenceCap: 0.3 } })), []);
    assert.deepEqual(types(one(as, undefined, { comparability: { nameMatchMin: 95, nameVariantMin: 85, unresolvedConfidenceCap: 0.3 } })), ["NAME_CONFLICT"]);
  });
});

describe("dates, ids, addresses, amounts", () => {
  it("dob: equal agrees, different is high, a day/month swap is its own milder type", () => {
    const dob = (a: string, b: string) => one([A("D1", "dob", a, "E1"), A("D2", "dob", b, "E1")]);
    assert.deepEqual(dob("2004-08-12", "2004-08-12"), []);
    const c = dob("2004-08-12", "2001-08-12");
    assert.deepEqual([c[0]?.type, c[0]?.severity, c[0]?.evidence[0]?.measurements?.["dayMonthSwap"]], ["DOB_CONFLICT", "high", "false"]);
    const s = dob("2004-08-12", "2004-12-08");
    assert.deepEqual([s[0]?.type, s[0]?.severity, s[0]?.evidence[0]?.measurements?.["dayMonthSwap"]], ["DOB_DAY_MONTH_SWAP", "medium", "true"]);
  });
  it("id_number: exact, ignoring case and spaces", () => {
    const id = (a: string, b: string) => one([A("D1", "id_number", a, "E1"), A("D2", "id_number", b, "E1")]);
    assert.deepEqual(id("TD-1", "td- 1"), []);
    assert.deepEqual(types(id("TD-1", "TD-2")), ["ID_NUMBER_CONFLICT"]);
  });
  it("addresses of the same kind: formatting differences agree, real differences are low severity", () => {
    const adr = (t: string, a: string, b: string) => one([A("D1", t, a, "E1"), A("D2", t, b, "E1")]);
    assert.deepEqual(adr("address_permanent", "14 Maple Court, Riverton", "14 MAPLE COURT RIVERTON"), []);
    const f = adr("address_current", "Flat 9, Lakeview", "22 Harbor Lane");
    assert.deepEqual([f[0]?.type, f[0]?.severity], ["ADDRESS_CONFLICT", "low"]);
  });
  it("permanent vs current address never produce a finding (MAY_DIFFER guard)", () => {
    const f = one([A("D1", "address_permanent", "14 Maple Court", "E1"), A("D2", "address_current", "Flat 9 Lakeview", "E1")]);
    assert.deepEqual(f, []);
    // remove the guard rule: the broader MUST_AGREE rule now compares them, proving the guard is what suppresses
    const without = DEFAULT_RULES.filter((r) => r.ruleType !== "MAY_DIFFER");
    assert.deepEqual(types(one([A("D1", "address_permanent", "14 Maple Court", "E1"), A("D2", "address_current", "Flat 9 Lakeview", "E1")], undefined, { rules: without })), ["ADDRESS_CONFLICT"]);
  });
  it("requested amount vs account balance never produce a finding (NOT_COMPARABLE guard)", () => {
    const broad: ComparabilityRule = { ruleId: "R-TEST-BROAD", ruleType: "MUST_AGREE", semanticTypes: ["requested_amount", "account_balance"], entityRoles: ["applicant"], matchMethod: "amount_exact", severity: "high", note: "x", findingType: "AMOUNT_CONFLICT" };
    const as = [A("D1", "requested_amount", "250000.00", "E1"), A("D2", "account_balance", "86430.10", "E1")];
    assert.deepEqual(one(as, undefined, { rules: [broad, ...DEFAULT_RULES] }), []);
    assert.deepEqual(types(one(as, undefined, { rules: [broad] })), ["AMOUNT_CONFLICT"]); // without the guard it would fire
  });
  it("amount_exact compares numerically on decimals", () => {
    const rule: ComparabilityRule = { ruleId: "R-T", ruleType: "MUST_AGREE", semanticTypes: ["requested_amount"], entityRoles: ["applicant"], matchMethod: "amount_exact", severity: "medium", note: "x", findingType: "AMOUNT_CONFLICT" };
    const amt = (a: string, b: string) => one([A("D1", "requested_amount", a, "E1"), A("D2", "requested_amount", b, "E1")], undefined, { rules: [rule] });
    assert.deepEqual(amt("100.00", "100.0"), []);
    assert.deepEqual(amt("100.00", "100.0000"), []);
    assert.deepEqual(types(amt("100.00", "100.01")), ["AMOUNT_CONFLICT"]);
  });
});

describe("scope of comparison", () => {
  it("values from the same document are not compared with each other", () => {
    assert.deepEqual(one([A("D1", "dob", "2004-08-12", "E1"), A("D1", "dob", "2001-08-12", "E1")]), []);
  });
  it("different RESOLVED entities are never compared", () => {
    const as = [A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", "1971-11-21", "E2")];
    assert.deepEqual(evaluate(as, [E("E1", "applicant", as), E("E2", "guarantor", as)]), []);
  });
  it("assertions with null or empty values are skipped, never treated as differences", () => {
    assert.deepEqual(one([A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", null, "E1"), A("D3", "dob", "", "E1")]), []);
  });
  it("a rule is skipped for entity roles it does not list", () => {
    const rule: ComparabilityRule = { ruleId: "R-T", ruleType: "MUST_AGREE", semanticTypes: ["dob"], entityRoles: ["guarantor"], matchMethod: "date_iso", severity: "high", note: "x", findingType: "DOB_CONFLICT" };
    const as = [A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", "2001-08-12", "E1")];
    assert.deepEqual(evaluate(as, [E("E1", "applicant", as)], { rules: [rule] }), []);
    assert.deepEqual(types(evaluate(as, [E("E1", "guarantor", as)], { rules: [rule] })), ["DOB_CONFLICT"]);
  });
  it("evidence holds both regions and the pair is ordered by input order", () => {
    const x = A("D1", "dob", "2004-08-12", "E1"), y = A("D2", "dob", "2001-08-12", "E1");
    const ents = [E("E1", "applicant", [x, y])];
    for (const as of [[x, y], [y, x]]) {
      const f = evaluate(as, ents)[0]!;
      assert.deepEqual(f.evidence[0]?.assertionIds, as.map((a) => a.assertionId));
      assert.deepEqual(f.evidence[0]?.regions, as.map((a) => a.region));
    }
  });
  it("confidence is the lower extraction confidence of the two assertions", () => {
    const f = one([A("D1", "dob", "2004-08-12", "E1", 0.95), A("D2", "dob", "2001-08-12", "E1", 0.81)]);
    assert.equal(f[0]?.confidence, 0.81);
    assert.notEqual(f[0]?.confidence, f[0]?.severity);
  });
});

describe("SUGGESTED links", () => {
  const setup = (conf = 0.9) => {
    const as = [A("D1", "full_name", "Aarav Kumar Sharma", "E1"), A("D2", "full_name", "Aarav Sharma", "E1"), A("D3", "full_name", "Aarav Sharma", "E2", conf)];
    return { as, ents: [E("E1", "applicant", as), E("E2", "account_holder", as, "SUGGESTED", ["E1"])] };
  };
  it("compares across the link, caps confidence at the configured value and says so", () => {
    const { as, ents } = setup();
    const f = evaluate(as, ents);
    assert.equal(f.length, 2); // E1 internal pair (D1,D2) and E2 vs E1: D3 vs D1 (variant), D3 vs D2 (equal -> none)
    const capped = f.find((x) => x.evidence[0]?.measurements?.["confidenceCapped"] === "true")!;
    assert.equal(capped.confidence, 0.3);
    assert.match(capped.evidence[0]?.description ?? "", /only a SUGGESTED link/);
    assert.equal(capped.evidence[0]?.measurements?.["entityResolution"], "SUGGESTED");
  });
  it("a confidence below the cap is kept, not raised", () => {
    const { as, ents } = setup(0.2);
    assert.ok(evaluate(as, ents).some((x) => x.confidence === 0.2));
  });
  it("without a suggestedLink there is no comparison across entities", () => {
    const { as, ents } = setup();
    const noLink = [ents[0]!, { ...ents[1]!, resolution: "UNRESOLVED" as const, suggestedLinks: undefined }];
    assert.equal(evaluate(as, noLink).length, 1);
  });
  it("two entities that suggest each other produce each pair once", () => {
    const as = [A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", "2001-08-12", "E2")];
    const ents = [E("E1", "unknown", as, "SUGGESTED", ["E2"]), E("E2", "unknown", as, "SUGGESTED", ["E1"])];
    assert.equal(evaluate(as, ents).length, 1);
  });
});

// ------------------------------------------------------------------------------------------------ context rules
describe("ARITHMETIC: debits and credits are checked separately", () => {
  const stmt = (credits: string[], creditTotal: string | null, debits: string[], debitTotal: string | null, extra: Array<[string, string]> = []) => {
    const as = [
      ...credits.map((v) => A("S1", "txn_amount", v, "E1")), ...(creditTotal === null ? [] : [A("S1", "statement_total", creditTotal, "E1")]),
      ...debits.map((v) => A("S1", "txn_debit_amount", v, "E1")), ...(debitTotal === null ? [] : [A("S1", "statement_debit_total", debitTotal, "E1")]),
      ...extra.map(([t, v]) => A("S1", t, v, "E1")),
    ];
    return evaluate(as, [E("E1", "account_holder", as)]);
  };
  it("matching credits and debits produce no finding", () => assert.deepEqual(stmt(["100.00", "20.50"], "120.50", ["10.00", "5.25"], "15.25"), []));
  it("a wrong DEBITS total is found (scenario S7a: printed 2205.10, rows sum to 2105.10)", () => {
    const f = stmt(["100.00"], "100.00", ["2000.00", "105.10"], "2205.10");
    assert.equal(f.length, 1);
    assert.deepEqual([f[0]?.type, f[0]?.observationStatus, f[0]?.evidence[0]?.measurements?.["ruleId"], f[0]?.evidence[0]?.measurements?.["difference"]],
      ["STATEMENT_TOTAL_MISMATCH", "INCONSISTENT", "R-STMT-DEBIT-ARITH", "100.00"]);
  });
  it("a wrong CREDITS total is still found by the credits rule only", () => {
    const f = stmt(["100.00"], "101.00", ["10.00"], "10.00");
    assert.deepEqual(f.map((x) => x.evidence[0]?.measurements?.["ruleId"]), ["R-STMT-TOTAL-ARITH"]);
  });
  it("debit rows are never summed into the credits total", () => assert.deepEqual(stmt(["100.00"], "100.00", ["50.00"], "50.00"), []));
  it("a bare 'total' (debits or credits, unknowable) never joins an arithmetic check", () => {
    assert.deepEqual(stmt(["100.00"], "100.00", ["50.00"], "50.00", [["total", "999.99"]]), []);
  });
  it("a debits total with no debit rows is INCONCLUSIVE, not silently fine", () => {
    const f = stmt(["100.00"], "100.00", [], "50.00");
    assert.deepEqual([f[0]?.type, f[0]?.observationStatus], ["STATEMENT_TOTAL_UNVERIFIABLE", "INCONCLUSIVE"]);
  });
  it("an unreadable debit row is INCONCLUSIVE", () => {
    const as = [A("S1", "txn_debit_amount", "10.00", "E1"), A("S1", "txn_debit_amount", null, "E1"), A("S1", "statement_debit_total", "10.00", "E1")];
    const f = evaluate(as, [E("E1", "account_holder", as)]);
    assert.deepEqual([f[0]?.type, f[0]?.observationStatus], ["STATEMENT_TOTAL_UNVERIFIABLE", "INCONCLUSIVE"]);
  });
});

describe("ARITHMETIC", () => {
  const stmt = (items: Array<string | null>, total: string | null, docId = "S1") => {
    const as = [...items.map((v) => A(docId, "txn_amount", v, "E1")), A(docId, "statement_total", total, "E1")];
    return { as, ents: [E("E1", "account_holder", as)] };
  };
  const run = (items: Array<string | null>, total: string | null, opts = {}) => { const { as, ents } = stmt(items, total); return evaluate(as, ents, opts); };
  it("a matching total produces no finding", () => assert.deepEqual(run(["10.00", "5.50"], "15.50"), []));
  it("a mismatch is INCONSISTENT with printed, computed and difference", () => {
    const f = run(["10.00", "5.50"], "16.50");
    assert.deepEqual([f[0]?.type, f[0]?.category, f[0]?.observationStatus, f[0]?.severity], ["STATEMENT_TOTAL_MISMATCH", "context", "INCONSISTENT", "medium"]);
    assert.deepEqual([f[0]?.evidence[0]?.measurements?.["computedTotal"], f[0]?.evidence[0]?.measurements?.["difference"]], ["15.50", "1.00"]);
  });
  it("sums exactly (no floating point drift) and handles negative rows", () => {
    assert.deepEqual(run(["0.10", "0.20", "-0.05"], "0.25"), []);
    assert.deepEqual(run(["1000000000000.01", "0.02"], "1000000000000.03"), []);
  });
  it("tolerance comes from config", () => {
    assert.equal(run(["10.00"], "10.01").length, 1);
    assert.equal(run(["10.00"], "10.01", { context: { arithmeticToleranceCents: 1 } }).length, 0);
  });
  it("an unreadable row makes the check INCONCLUSIVE, never silently fine", () => {
    const f = run(["10.00", null], "10.00");
    assert.deepEqual([f[0]?.type, f[0]?.observationStatus, f[0]?.severity], ["STATEMENT_TOTAL_UNVERIFIABLE", "INCONCLUSIVE", "info"]);
    assert.match(f[0]?.evidence[0]?.description ?? "", /some listed amounts could not be read/);
  });
  it("a printed total with no rows is INCONCLUSIVE too", () => {
    const f = run([], "10.00");
    assert.equal(f[0]?.observationStatus, "INCONCLUSIVE");
    assert.match(f[0]?.evidence[0]?.description ?? "", /no listed amounts were found/);
  });
  it("a null total is ignored, and documents are checked separately", () => {
    assert.deepEqual(run(["10.00"], null), []);
    const a = stmt(["10.00"], "11.00", "S1"), b = stmt(["10.00"], "10.00", "S2");
    const f = evaluate([...a.as, ...b.as], [E("E1", "account_holder", [...a.as, ...b.as])]);
    assert.equal(f.length, 1);
    assert.equal(f[0]?.evidence[0]?.regions[0]?.docId, "S1");
  });
  it("provenance is a single document's own hash", () => {
    const { as, ents } = stmt(["10.00"], "11.00");
    assert.equal(evaluate(as, ents, { docSha256: { S1: "ab".repeat(32) } })[0]?.evidence[0]?.provenance.inputSha256, "ab".repeat(32));
  });
});

describe("TEMPORAL", () => {
  const t = (dob: string, issue: string) => one([A("D1", "dob", dob, "E1"), A("D1", "issue_date", issue, "E1")]);
  it("dob before the issue date is fine", () => assert.deepEqual(t("2004-08-12", "2023-02-03"), []));
  it("dob after the issue date is flagged at high severity", () => {
    const f = t("2025-01-01", "2023-02-03");
    assert.deepEqual([f[0]?.type, f[0]?.category, f[0]?.severity, f[0]?.observationStatus], ["DOB_NOT_BEFORE_ISSUE_DATE", "context", "high", "INCONSISTENT"]);
  });
  it("the same date is also flagged (strictly before)", () => assert.deepEqual(types(t("2023-02-03", "2023-02-03")), ["DOB_NOT_BEFORE_ISSUE_DATE"]));
  it("works across documents of the same entity and checks the application date too", () => {
    const f = one([A("D1", "dob", "2030-01-01", "E1"), A("D2", "application_date", "2026-10-15", "E1")]);
    assert.deepEqual(types(f), ["DOB_NOT_BEFORE_APPLICATION_DATE"]);
  });
  it("null and non-ISO values are skipped", () => {
    assert.deepEqual(one([A("D1", "dob", null, "E1"), A("D1", "issue_date", "2023-02-03", "E1")]), []);
    assert.deepEqual(one([A("D1", "dob", "12/08/2004", "E1"), A("D1", "issue_date", "2001-02-03", "E1")]), []);
  });
});

describe("UNIQUENESS", () => {
  const two = (a: string, b: string, t = "id_number") => {
    const as = [A("D1", t, a, "E1"), A("D2", t, b, "E2")];
    return evaluate(as, [E("E1", "applicant", as), E("E2", "guarantor", as)]);
  };
  it("the same id_number under two entities is a potential anomaly", () => {
    const f = two("TD-1", "td-1");
    assert.deepEqual([f[0]?.type, f[0]?.observationStatus, f[0]?.severity, f[0]?.category], ["ID_NUMBER_SHARED", "POTENTIAL_ANOMALY", "high", "cross_document"]);
    assert.equal(f[0]?.evidence[0]?.regions.length, 2);
    assert.match(f[0]?.evidence[0]?.description ?? "", /applicant and guarantor/);
  });
  it("different values, or one entity across documents, produce nothing", () => {
    assert.deepEqual(two("TD-1", "TD-2"), []);
    assert.deepEqual(one([A("D1", "id_number", "TD-1", "E1"), A("D2", "id_number", "TD-1", "E1")]), []);
  });
  it("account_number sharing is medium severity", () => assert.equal(two("AC-1", "AC-1", "account_number")[0]?.severity, "medium"));
  it("assertions without an entity are ignored", () => {
    const as = [A("D1", "id_number", "TD-1", "E1"), { ...A("D2", "id_number", "TD-1", "E2"), entityId: null }];
    assert.deepEqual(evaluate(as, [E("E1", "applicant", as)]), []);
  });
});

describe("contract of the function", () => {
  it("is deterministic and never mutates its inputs (including the rule table)", () => {
    const { assertions, entities } = runCase("CASE-001");
    const a = deepFreeze(structuredClone(assertions)), e = deepFreeze(structuredClone(entities)), rules = deepFreeze(structuredClone(DEFAULT_RULES));
    assert.deepStrictEqual(evaluate(a, e, { rules }), evaluate(a, e, { rules }));
  });
  it("empty input gives no findings", () => assert.deepEqual(evaluate([], []), []));
  it("numbers findings and evidence in rule-table order and can continue a numbering", () => {
    const { findings } = runCase("CASE-001");
    assert.deepEqual(findings.map((f) => f.findingId), ["F-0001", "F-0002", "F-0003", "F-0004"]);
    assert.deepEqual(nextIdStart(findings), { finding: 5, evidence: 5 });
    const as = [A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", "2001-08-12", "E1")];
    const f = evaluate(as, [E("E1", "applicant", as)], { idStart: { finding: 5, evidence: 5 } });
    assert.deepEqual([f[0]?.findingId, f[0]?.evidence[0]?.evidenceId], ["F-0005", "E-0005"]);
    assert.deepEqual(nextIdStart([]), { finding: 1, evidence: 1 });
  });
  it("provenance echoes the thresholds used and the digest of the documents involved", () => {
    const f = one([A("D1", "dob", "2004-08-12", "E1"), A("D2", "dob", "2001-08-12", "E1")], undefined, { docSha256: { D1: "aa".repeat(32), D2: "bb".repeat(32) }, processedAt: "2026-01-02T03:04:05Z" });
    const p = f[0]!.evidence[0]!.provenance;
    assert.deepEqual(p.config, { nameMatchMin: 92, nameVariantMin: 75, unresolvedConfidenceCap: 0.3 });
    assert.equal(p.processedAt, "2026-01-02T03:04:05Z");
    assert.match(p.inputSha256, /^[0-9a-f]{64}$/);
    assert.notEqual(p.inputSha256, "aa".repeat(32));
  });
});
