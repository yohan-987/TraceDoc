import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import type { ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, DocumentAssertion, ExtractedField, PageRecord } from "../../../contracts/types";
import {
  assertionIdFor, buildAssertions, classifyFieldName, normalizeAmount, normalizeDate, normalizeIdentifier, normalizeName, provisionalRole,
} from "./assertions";

const fx = resolve(dirname(fileURLToPath(import.meta.url)), "../../../contracts/fixtures");
const readJson = <T>(p: string): T => JSON.parse(readFileSync(resolve(fx, p), "utf8")) as T;
const CASES: Record<string, string[]> = { "CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"] };

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object") { Object.values(o as object).forEach(deepFreeze); Object.freeze(o); }
  return o;
}

function loadCase(caseId: string) {
  const docs = (CASES[caseId] ?? []).map((d) => readJson<AnalyzeResponse>(`analyze/${d}.json`));
  return { fields: docs.flatMap((d) => d.fields), pages: docs.flatMap((d) => d.pages), expected: readJson<ReasoningOutput>(`${caseId}.reasoning.json`).assertions };
}

const strip = (a: DocumentAssertion) => { const { entityId, entityRole, ...rest } = a; return rest; };

describe("fixtures", () => {
  for (const caseId of Object.keys(CASES)) {
    it(`${caseId}: fixture fields produce the fixture assertions`, () => {
      const { fields, pages, expected } = loadCase(caseId);
      const got = buildAssertions(fields, pages, caseId);
      assert.equal(got.length, expected.length);
      got.forEach((a, i) => assert.deepStrictEqual(strip(a), strip(expected[i]!), `assertion ${expected[i]?.assertionId}`));
    });

    it(`${caseId}: entityId is null and the role is provisional (docType or prefix only)`, () => {
      const { fields, pages } = loadCase(caseId);
      const typeOf = new Map(pages.map((p) => [p.docId, p.docType]));
      for (const a of buildAssertions(fields, pages, caseId)) {
        assert.equal(a.entityId, null);
        const prefixed = a.fieldId && fields.find((f) => f.fieldId === a.fieldId)!.name.startsWith("guarantor_");
        const want = prefixed ? "guarantor" : typeOf.get(a.docId) === "application_form" ? "applicant" : typeOf.get(a.docId) === "bank_statement" ? "account_holder" : "unknown";
        assert.equal(a.entityRole, want, a.assertionId);
      }
    });
  }

  it("is deterministic, pure, and never mutates or aliases its input", () => {
    const { fields, pages } = loadCase("CASE-001");
    const frozenF = deepFreeze(structuredClone(fields)), frozenP = deepFreeze(structuredClone(pages));
    const a = buildAssertions(frozenF, frozenP, "CASE-001"), b = buildAssertions(frozenF, frozenP, "CASE-001");
    assert.deepStrictEqual(a, b);
    a[0]!.region.bbox[0] = 0.999; // output must not alias input
    assert.notEqual(frozenF[0]!.region.bbox[0], 0.999);
  });
});

describe("field name mapping", () => {
  it("maps the O4 extractor names for bank statements to canonical semantic types", () => {
    assert.equal(classifyFieldName("account_holder").semanticType, "full_name");
    assert.equal(classifyFieldName("closing_balance").semanticType, "account_balance");
    assert.equal(classifyFieldName("total_credits").semanticType, "statement_total");
    assert.equal(classifyFieldName("total_debits").semanticType, "statement_debit_total");
    assert.equal(classifyFieldName("debit_amount").semanticType, "txn_debit_amount");
    // a bare "total" can be the debits or the credits column: it stays unmapped so it never joins an arithmetic check
    assert.equal(classifyFieldName("total").semanticType, "total");
    assert.equal(classifyFieldName("opening_balance").semanticType, "opening_balance");
  });
  it("handles case, spaces and dashes, and passes unknown names through", () => {
    assert.equal(classifyFieldName("Date of Birth").semanticType, "dob");
    assert.equal(classifyFieldName("Permanent-Address").semanticType, "address_permanent");
    assert.deepEqual(classifyFieldName("blood_group"), { semanticType: "blood_group", prefixRole: null });
  });
  it("reads role prefixes", () => {
    assert.deepEqual(classifyFieldName("guarantor_full_name"), { semanticType: "full_name", prefixRole: "guarantor" });
    assert.deepEqual(classifyFieldName("co_applicant_dob"), { semanticType: "dob", prefixRole: "co_applicant" });
    assert.deepEqual(classifyFieldName("guarantor_id_number"), { semanticType: "id_number", prefixRole: "guarantor" });
    assert.deepEqual(classifyFieldName("applicant_name"), { semanticType: "full_name", prefixRole: "applicant" });
  });
  it("provisional roles", () => {
    assert.equal(provisionalRole("id_card", null), "unknown");
    assert.equal(provisionalRole("application_form", null), "applicant");
    assert.equal(provisionalRole("bank_statement", null), "account_holder");
    assert.equal(provisionalRole("UNKNOWN", null), "unknown");
    assert.equal(provisionalRole(undefined, null), "unknown");
    assert.equal(provisionalRole("application_form", "guarantor"), "guarantor");
  });
  it("assertion ids follow the field id", () => {
    assert.equal(assertionIdFor("D-01-F02"), "D-01-A02");
    assert.equal(assertionIdFor("weird"), "A-weird");
  });
});

describe("normalizers", () => {
  it("dates", () => {
    assert.equal(normalizeDate("12/08/2004"), "2004-08-12");
    assert.equal(normalizeDate("2004-08-12"), "2004-08-12");
    assert.equal(normalizeDate("12-8-2004"), "2004-08-12");
    assert.equal(normalizeDate("12.08.2004"), "2004-08-12");
    assert.equal(normalizeDate("2004/08/12"), "2004-08-12");
    assert.equal(normalizeDate("12 Aug 2004"), "2004-08-12");
    assert.equal(normalizeDate("August 12, 2004"), "2004-08-12");
    assert.equal(normalizeDate("3rd Feb 2023"), "2023-02-03");
    assert.equal(normalizeDate("25/12/2020"), "2020-12-25"); // day cannot be a month
    assert.equal(normalizeDate("12/25/2020"), "2020-12-25"); // second part cannot be a month: month-first
    assert.equal(normalizeDate("05/03/1996"), "1996-03-05"); // ambiguous: day-first
  });
  it("rejects impossible or ambiguous dates instead of guessing", () => {
    for (const bad of ["31/02/2020", "13/13/2020", "12/08/04", "", "   ", "yesterday", "00/00/0000", "2020-02-30", "1/1/99"]) assert.equal(normalizeDate(bad), null, bad);
    assert.equal(normalizeDate(null), null);
    assert.equal(normalizeDate(undefined), null);
  });
  it("amounts", () => {
    assert.equal(normalizeAmount("250,000.00"), "250000.00");
    assert.equal(normalizeAmount("2,50,000"), "250000.00");
    assert.equal(normalizeAmount("Rs. 250000"), "250000.00");
    assert.equal(normalizeAmount("₹ 1,250.5"), "1250.50");
    assert.equal(normalizeAmount("INR 250000/-"), "250000.00");
    assert.equal(normalizeAmount("(1,200.50)"), "-1200.50");
    assert.equal(normalizeAmount("-1200.5"), "-1200.50");
    assert.equal(normalizeAmount("1,200.50 DR"), "-1200.50");
    assert.equal(normalizeAmount("1,200.50 CR"), "1200.50");
    assert.equal(normalizeAmount(".50"), "0.50");
    assert.equal(normalizeAmount("000123.4"), "123.40");
    assert.equal(normalizeAmount("1.234,56"), "1234.56");
    assert.equal(normalizeAmount("0.00"), "0.00");
    assert.equal(normalizeAmount("-0.00"), "0.00");
    assert.equal(normalizeAmount("12345678901234567890.12"), "12345678901234567890.12"); // no float rounding
  });
  it("rejects non-amounts", () => {
    for (const bad of ["abc", "", "--", "1.2.3", "12a4", "Rs.", "."]) assert.equal(normalizeAmount(bad), null, bad);
  });
  it("names", () => {
    assert.equal(normalizeName("AARAV KUMAR SHARMA"), "Aarav Kumar Sharma");
    assert.equal(normalizeName("  aarav   sharma "), "Aarav Sharma");
    assert.equal(normalizeName("O'BRIEN"), "O'Brien");
    assert.equal(normalizeName("mary-ann smith"), "Mary-Ann Smith");
    assert.equal(normalizeName("a. k. sharma"), "A. K. Sharma");
    assert.equal(normalizeName("   "), null);
  });
  it("identifiers", () => {
    assert.equal(normalizeIdentifier("td-48213-77"), "TD-48213-77");
    assert.equal(normalizeIdentifier("TD 48213 77"), "TD4821377");
    assert.equal(normalizeIdentifier("TD–48213–77"), "TD-48213-77");
    assert.equal(normalizeIdentifier(""), null);
  });
});

function field(over: Partial<ExtractedField> & { fieldId: string; name: string; rawValue: string }): ExtractedField {
  return {
    docId: "X-1", normalizedValue: null, ocrConfidence: 0.9, extractionConfidence: 0.8, validationStatus: "NOT_CHECKED",
    region: { docId: "X-1", page: 1, bbox: [0.1, 0.1, 0.2, 0.05] }, source: "ocr", ...over,
  };
}
const page = (docType: PageRecord["docType"], docId = "X-1", n = 1): PageRecord =>
  ({ docId, page: n, renderPath: "x.png", renderDpi: 150, rotationDeg: 0, deskewDeg: 0, docType, docTypeConfidence: 0.9 });

describe("edge cases", () => {
  it("keeps every field: unparseable values become null, nothing is dropped", () => {
    const f = [field({ fieldId: "X-1-F01", name: "dob", rawValue: "??/??/20O4", validationStatus: "INVALID_FORMAT" }),
               field({ fieldId: "X-1-F02", name: "requested_amount", rawValue: "lots" }),
               field({ fieldId: "X-1-F03", name: "full_name", rawValue: "" })];
    const out = buildAssertions(f, [page("application_form")], "C");
    assert.equal(out.length, 3);
    assert.deepEqual(out.map((a) => a.normalizedValue), [null, null, null]);
    assert.deepEqual(out.map((a) => a.extractionConfidence), [0.8, 0.8, 0.8]);
  });
  it("uses the extractor's normalizedValue first and falls back to the raw text", () => {
    const f = [field({ fieldId: "X-1-F01", name: "dob", rawValue: "12/08/2004", normalizedValue: "2004-08-12" }),
               field({ fieldId: "X-1-F02", name: "dob", rawValue: "05/03/1996", normalizedValue: "garbage" })];
    assert.deepEqual(buildAssertions(f, [page("id_card")], "C").map((a) => a.normalizedValue), ["2004-08-12", "1996-03-05"]);
  });
  it("document and effective dates: preference by docType, effective date for balances, explicit effective_date wins", () => {
    const f = [field({ fieldId: "X-1-F01", name: "statement_date", rawValue: "30/09/2026" }),
               field({ fieldId: "X-1-F02", name: "closing_balance", rawValue: "100.00" }),
               field({ fieldId: "X-1-F03", name: "account_holder", rawValue: "A B" })];
    const [d, bal, who] = buildAssertions(f, [page("bank_statement")], "C") as [DocumentAssertion, DocumentAssertion, DocumentAssertion];
    assert.equal(d.documentDate, "2026-09-30");
    assert.equal(bal.documentDate, "2026-09-30");
    assert.equal(bal.effectiveDate, "2026-09-30");
    assert.equal("effectiveDate" in who, false);
    const g = [...f, field({ fieldId: "X-1-F04", name: "effective_date", rawValue: "01/10/2026" })];
    assert.equal(buildAssertions(g, [page("bank_statement")], "C")[2]?.effectiveDate, "2026-10-01");
  });
  it("omits date properties when the document states no date", () => {
    const a = buildAssertions([field({ fieldId: "X-1-F01", name: "full_name", rawValue: "a b" })], [page("id_card")], "C")[0]!;
    assert.equal("documentDate" in a, false);
    assert.equal("effectiveDate" in a, false);
  });
  it("uses the docType of the field's own page in a multi-page document", () => {
    const f = [field({ fieldId: "X-1-F01", name: "full_name", rawValue: "a b" }),
               field({ fieldId: "X-1-F02", name: "full_name", rawValue: "c d", region: { docId: "X-1", page: 2, bbox: [0.1, 0.1, 0.1, 0.1] } })];
    const out = buildAssertions(f, [page("application_form", "X-1", 1), page("bank_statement", "X-1", 2)], "C");
    assert.deepEqual(out.map((a) => a.entityRole), ["applicant", "account_holder"]);
  });
  it("a missing page record gives role unknown, not an error", () => {
    assert.equal(buildAssertions([field({ fieldId: "X-1-F01", name: "full_name", rawValue: "a b" })], [], "C")[0]?.entityRole, "unknown");
  });
  it("empty input gives empty output; duplicate field ids are a programming error", () => {
    assert.deepEqual(buildAssertions([], [], "C"), []);
    const dup = field({ fieldId: "X-1-F01", name: "dob", rawValue: "1" });
    assert.throws(() => buildAssertions([dup, dup], [], "C"), /duplicate fieldId/);
  });
  it("keeps input order and per-document dates separate", () => {
    const f = [field({ fieldId: "A-F01", docId: "A", name: "issue_date", rawValue: "01/01/2020", region: { docId: "A", page: 1, bbox: [0, 0, 0.1, 0.1] } }),
               field({ fieldId: "B-F01", docId: "B", name: "issue_date", rawValue: "02/02/2021", region: { docId: "B", page: 1, bbox: [0, 0, 0.1, 0.1] } })];
    const out = buildAssertions(f, [page("id_card", "A"), page("id_card", "B")], "C");
    assert.deepEqual(out.map((a) => [a.docId, a.documentDate]), [["A", "2020-01-01"], ["B", "2021-02-02"]]);
  });
});

describe("ids are unique per document, as the Python service numbers them", () => {
  const f = (docId: string, fieldId: string, name: string, raw: string) => field({ docId, fieldId, name, rawValue: raw, region: { docId, page: 1, bbox: [0.1, 0.1, 0.2, 0.05] } });
  it("the same field id in two documents gives two different assertion ids (the real service emits FLD-001 in every document)", () => {
    const out = buildAssertions([f("D-1", "FLD-001", "dob", "01/02/2000"), f("D-2", "FLD-001", "dob", "01/02/2000")], [page("id_card", "D-1"), page("application_form", "D-2")], "C");
    assert.deepEqual(out.map((a) => a.assertionId), ["A-D-1-FLD-001", "A-D-2-FLD-001"]);
    assert.deepEqual(out.map((a) => a.fieldId), ["FLD-001", "FLD-001"]);
  });
  it("the same field id twice in one document is still an error", () => {
    assert.throws(() => buildAssertions([f("D-1", "FLD-001", "dob", "1"), f("D-1", "FLD-001", "full_name", "a b")], [], "C"), /duplicate fieldId FLD-001 in document D-1/);
  });
  it("fixture-style ids keep their short form, and a prefix of another document does not", () => {
    assert.equal(assertionIdFor("D-01-F02", "D-01"), "D-01-A02");
    assert.equal(assertionIdFor("D-01-F02", "D-02"), "A-D-02-D-01-F02");
    assert.equal(assertionIdFor("D-01-F02"), "D-01-A02");
    assert.equal(assertionIdFor("FLD-001", "D-7"), "A-D-7-FLD-001");
  });
  it("two fields that would collide on the assertion id are refused, never silently merged", () => {
    assert.throws(() => buildAssertions([f("X-Y", "FLD-001", "dob", "1"), f("X", "Y-FLD-001", "dob", "1")], [], "C"), /same assertionId/);
  });
});
