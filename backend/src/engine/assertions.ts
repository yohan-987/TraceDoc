/**
 * B4: assertion builder. Pure: no I/O, no clocks, no randomness, never mutates its input.
 *
 * buildAssertions(fields, pages, caseId) turns extracted fields into DocumentAssertions:
 *  - maps the extractor's field name to a canonical semanticType (aliases below),
 *  - assigns a PROVISIONAL entityRole from the page docType (applicant on application_form,
 *    account_holder on bank_statement, unknown on id_card until the entity resolver decides),
 *    or from a role prefix in the field name (guarantor_full_name -> guarantor),
 *  - normalizes values (ISO dates, plain decimal amounts, name casing, upper-case ids),
 *  - carries documentDate and effectiveDate when the document states them.
 *
 * Nothing is dropped: a field whose value cannot be normalized still becomes an assertion with
 * normalizedValue null, so downstream rules skip it instead of guessing. extractionConfidence and the
 * region are carried unchanged. entityId is always null here; B5 sets it.
 */
import type { DocType, DocumentAssertion, EntityRole, ExtractedField, PageRecord } from "../../../contracts/types";

// ------------------------------------------------------------------------------------------ semantic types
/** canonical semanticType -> accepted extractor field names (already lower_snake). Canonical names are the fixture names. */
const CANONICAL_ALIASES: Record<string, string[]> = {
  full_name: ["full_name", "name", "account_holder", "account_holder_name", "holder_name"],
  dob: ["dob", "date_of_birth", "birth_date", "birthdate"],
  id_number: ["id_number", "id_no", "national_id"],
  address_permanent: ["address_permanent", "permanent_address"],
  address_current: ["address_current", "current_address", "residential_address", "present_address"],
  issue_date: ["issue_date", "date_of_issue", "issued_on"],
  application_date: ["application_date", "date_of_application"],
  requested_amount: ["requested_amount", "loan_amount", "amount_requested", "requested_loan_amount"],
  account_number: ["account_number", "account_no", "acct_number"],
  account_balance: ["account_balance", "closing_balance"],
  opening_balance: ["opening_balance"],
  // a bare "total" is deliberately NOT an alias: on a statement it can be the debits or the credits column, so it never joins an arithmetic check
  statement_total: ["statement_total", "total_deposits", "total_credits"],
  statement_debit_total: ["statement_debit_total", "total_debits", "total_withdrawals"],
  statement_date: ["statement_date", "statement_period_end", "as_of_date"],
  txn_amount: ["txn_amount", "transaction_amount", "row_amount"], // credit rows, checked against statement_total
  txn_debit_amount: ["txn_debit_amount", "debit_amount", "transaction_debit_amount"], // debit rows, checked against statement_debit_total
  effective_date: ["effective_date"],
  document_date: ["document_date"],
};

const ALIAS_TO_CANONICAL: ReadonlyMap<string, string> = new Map(
  Object.entries(CANONICAL_ALIASES).flatMap(([canon, names]) => names.map((n) => [n, canon] as const)),
);

/** Role prefixes in extractor field names, e.g. guarantor_full_name. Longest prefix first. */
const ROLE_PREFIXES: ReadonlyArray<readonly [string, EntityRole]> = [
  ["co_applicant_", "co_applicant"],
  ["guarantor_", "guarantor"],
  ["nominee_", "nominee"],
  ["applicant_", "applicant"],
];

const fieldKey = (name: string): string => name.trim().toLowerCase().replace(/[\s-]+/g, "_");

/** Splits an extractor field name into its semanticType and an optional role prefix. */
export function classifyFieldName(name: string): { semanticType: string; prefixRole: EntityRole | null } {
  const key = fieldKey(name);
  const direct = ALIAS_TO_CANONICAL.get(key);
  if (direct) return { semanticType: direct, prefixRole: null };
  for (const [prefix, role] of ROLE_PREFIXES) {
    if (key.startsWith(prefix) && key.length > prefix.length) {
      const rest = key.slice(prefix.length);
      return { semanticType: ALIAS_TO_CANONICAL.get(rest) ?? rest, prefixRole: role };
    }
  }
  return { semanticType: key, prefixRole: null };
}

/** Provisional role only. An id_card is unknown until the resolver links it to an entity. */
export function provisionalRole(docType: DocType | undefined, prefixRole: EntityRole | null): EntityRole {
  if (prefixRole) return prefixRole;
  if (docType === "application_form") return "applicant";
  if (docType === "bank_statement") return "account_holder";
  return "unknown";
}

// ------------------------------------------------------------------------------------------ normalizers
const squash = (s: string): string => s.normalize("NFC").replace(/\s+/g, " ").trim();

const MONTHS: Record<string, number> = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

function isoIfReal(y: number, m: number, d: number): string | null {
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d) || y < 1000 || y > 9999) return null;
  const t = new Date(Date.UTC(y, m - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) return null;
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

/**
 * Parses a date to ISO yyyy-mm-dd, or null. Never guesses a two-digit year. For numeric d/m/y forms it assumes
 * day-first unless the second part cannot be a month (then month-first); if neither part can be a day or month it is null.
 */
export function normalizeDate(input: string | null | undefined): string | null {
  if (input == null) return null;
  const s = squash(input);
  if (!s) return null;
  const month = (name: string): number => MONTHS[name.slice(0, 3).toLowerCase()] ?? 0;
  let m = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(s);
  if (m) return isoIfReal(Number(m[1]), Number(m[2]), Number(m[3]));
  m = /^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/.exec(s);
  if (m) {
    const a = Number(m[1]), b = Number(m[2]), y = Number(m[3]);
    if (a > 12) return isoIfReal(y, b, a);
    if (b > 12) return isoIfReal(y, a, b);
    return isoIfReal(y, b, a); // both <= 12: day-first
  }
  m = /^(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})\.?,?[\s-]+(\d{4})$/.exec(s);
  if (m && month(m[2] ?? "")) return isoIfReal(Number(m[3]), month(m[2] ?? ""), Number(m[1]));
  m = /^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$/.exec(s);
  if (m && month(m[1] ?? "")) return isoIfReal(Number(m[3]), month(m[1] ?? ""), Number(m[2]));
  return null;
}

/**
 * Parses an amount to a plain decimal string with at least two decimals ("250000.00", "-1200.50"), or null.
 * Uses string handling only (no floats). Commas are thousands separators unless the text is clearly European (1.234,56).
 */
export function normalizeAmount(input: string | null | undefined): string | null {
  if (input == null) return null;
  let s = squash(input);
  if (!s) return null;
  let negative = false;
  if (/^\(.*\)$/.test(s)) { negative = true; s = s.slice(1, -1); }
  if (/\bdr\.?\s*$/i.test(s)) { negative = true; s = s.replace(/\bdr\.?\s*$/i, ""); }
  s = s.replace(/\bcr\.?\s*$/i, "").replace(/\b(?:rs|inr|usd|eur|gbp)\b\.?/gi, " ").replace(/[₹$€£¥]/g, " ").replace(/\/-\s*$/, "");
  s = s.trim();
  if (s.startsWith("-")) { negative = !negative; s = s.slice(1).trim(); } else if (s.startsWith("+")) s = s.slice(1).trim();
  if (!/^[\d.,\s]+$/.test(s) || !/\d/.test(s)) return null;
  s = s.replace(/\s+/g, "");
  if (/^\d{1,3}(\.\d{3})*,\d{1,2}$/.test(s)) s = s.replace(/\./g, "").replace(",", ".");
  else s = s.replace(/,/g, "");
  const m = /^(\d*)(?:\.(\d*))?$/.exec(s);
  if (!m || (m[1] === "" && !m[2])) return null;
  const whole = (m[1] ?? "").replace(/^0+(?=\d)/, "") || "0";
  const frac = (m[2] ?? "").padEnd(2, "0");
  const isZero = /^0*$/.test(whole + frac);
  return `${negative && !isZero ? "-" : ""}${whole}.${frac}`;
}

/** Whitespace squashed, Title Case, including after hyphens, apostrophes and full stops ("O'BRIEN" -> "O'Brien"). */
export function normalizeName(input: string | null | undefined): string | null {
  if (input == null) return null;
  const s = squash(input);
  if (!s) return null;
  return s.toLowerCase().replace(/(^|[\s\-'’.])(\p{L})/gu, (_, sep: string, ch: string) => sep + ch.toUpperCase());
}

/** Identifiers: no whitespace, upper case, typographic dashes become "-". */
export function normalizeIdentifier(input: string | null | undefined): string | null {
  if (input == null) return null;
  const s = input.normalize("NFC").replace(/[‐‑–—]/g, "-").replace(/\s+/g, "").toUpperCase();
  return s || null;
}

export function normalizeText(input: string | null | undefined): string | null {
  if (input == null) return null;
  return squash(input) || null;
}

export type ValueKind = "date" | "amount" | "name" | "identifier" | "text";

export function valueKindOf(semanticType: string): ValueKind {
  if (semanticType === "full_name") return "name";
  if (semanticType === "id_number" || semanticType === "account_number") return "identifier";
  if (semanticType === "dob" || semanticType.endsWith("_date")) return "date";
  if (/(_amount|_balance|_total)$/.test(semanticType)) return "amount";
  return "text";
}

/** Normalizes using the extractor's normalizedValue first, then the raw text; null when neither parses. */
export function normalizeValue(semanticType: string, field: Pick<ExtractedField, "rawValue" | "normalizedValue">): string | null {
  const fn = { date: normalizeDate, amount: normalizeAmount, name: normalizeName, identifier: normalizeIdentifier, text: normalizeText }[valueKindOf(semanticType)];
  return fn(field.normalizedValue) ?? fn(field.rawValue);
}

// ------------------------------------------------------------------------------------------ document level dates
const DOC_DATE_PREFERENCE: Record<string, string[]> = {
  id_card: ["issue_date", "document_date"],
  application_form: ["application_date", "document_date"],
  bank_statement: ["statement_date", "document_date"],
};
const DOC_DATE_FALLBACK = ["issue_date", "application_date", "statement_date", "document_date"];
/** Balances and totals apply as of the statement date unless the document gives an explicit effective_date. */
const EFFECTIVE_AT_STATEMENT_DATE = new Set(["account_balance", "statement_total", "statement_debit_total"]);

function docLevelDates(docFields: Array<{ semanticType: string; field: ExtractedField }>, docType: DocType | undefined) {
  const first = (type: string): string | null => {
    for (const { semanticType, field } of docFields) {
      if (semanticType !== type) continue;
      const iso = normalizeValue(type, field);
      if (iso) return iso;
    }
    return null;
  };
  let documentDate: string | null = null;
  for (const t of [...(DOC_DATE_PREFERENCE[docType ?? ""] ?? []), ...DOC_DATE_FALLBACK]) {
    documentDate = first(t);
    if (documentDate) break;
  }
  return { documentDate, effectiveDate: first("effective_date"), statementDate: first("statement_date") };
}

// ------------------------------------------------------------------------------------------ main
/**
 * assertionId for a field. Field ids only have to be unique within their document (the Python service numbers them per document:
 * FLD-001, FLD-002, ...), so the assertion id adds the document when the field id does not already carry it:
 *   fieldId "D-01-F02" in document D-01 -> "D-01-A02"      (the id convention of the fixtures)
 *   fieldId "FLD-001"  in document D-7    -> "A-D-7-FLD-001"
 * Without a docId (legacy call): "D-01-F02" -> "D-01-A02", anything else -> "A-<fieldId>".
 */
export function assertionIdFor(fieldId: string, docId?: string): string {
  if (docId === undefined) return /-F\d+$/.test(fieldId) ? fieldId.replace(/-F(\d+)$/, "-A$1") : `A-${fieldId}`;
  const prefix = `${docId}-F`;
  const digits = fieldId.startsWith(prefix) ? fieldId.slice(prefix.length) : "";
  return /^\d+$/.test(digits) ? `${docId}-A${digits}` : `A-${docId}-${fieldId}`;
}

export function buildAssertions(fields: readonly ExtractedField[], pages: readonly PageRecord[], caseId: string): DocumentAssertion[] {
  const pageType = new Map<string, DocType>(pages.map((p) => [`${p.docId}#${p.page}`, p.docType]));
  const firstPageType = new Map<string, DocType>();
  for (const p of [...pages].sort((a, b) => a.page - b.page)) if (!firstPageType.has(p.docId)) firstPageType.set(p.docId, p.docType);

  const seen = new Set<string>();
  const classified = fields.map((field) => {
    const key = `${field.docId}\u0000${field.fieldId}`; // (docId, fieldId) identifies a field
    if (seen.has(key)) throw new Error(`buildAssertions: duplicate fieldId ${field.fieldId} in document ${field.docId}`);
    seen.add(key);
    return { field, ...classifyFieldName(field.name) };
  });

  const byDoc = new Map<string, typeof classified>();
  for (const c of classified) byDoc.set(c.field.docId, [...(byDoc.get(c.field.docId) ?? []), c]);
  const dates = new Map<string, ReturnType<typeof docLevelDates>>();
  for (const [docId, list] of byDoc) dates.set(docId, docLevelDates(list, firstPageType.get(docId)));

  const built = classified.map(({ field, semanticType, prefixRole }) => {
    const d = dates.get(field.docId)!;
    const docType = pageType.get(`${field.docId}#${field.region.page}`) ?? firstPageType.get(field.docId);
    const effectiveDate = d.effectiveDate ?? (EFFECTIVE_AT_STATEMENT_DATE.has(semanticType) ? d.statementDate : null);
    const a: DocumentAssertion = {
      assertionId: assertionIdFor(field.fieldId, field.docId),
      caseId,
      docId: field.docId,
      fieldId: field.fieldId,
      entityId: null,
      entityRole: provisionalRole(docType, prefixRole),
      semanticType,
      normalizedValue: normalizeValue(semanticType, field),
      extractionConfidence: field.extractionConfidence,
      region: { ...field.region, bbox: [...field.region.bbox] as typeof field.region.bbox },
    };
    if (d.documentDate) a.documentDate = d.documentDate;
    if (effectiveDate) a.effectiveDate = effectiveDate;
    return a;
  });
  const ids = new Set(built.map((a) => a.assertionId));
  if (ids.size !== built.length) throw new Error("buildAssertions: two fields produced the same assertionId");
  return built;
}
