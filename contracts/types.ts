/**
 * TraceDocs shared contracts (source of truth: ARCHITECTURE.md section 5).
 * Types only, no logic. Contract changes go through Person 1: update contracts/ and fixtures in one commit.
 * All bounding boxes are normalized (0 to 1) to the CANONICAL RENDER of a page.
 */

export type ObservationStatus = "OBSERVED" | "INCONSISTENT" | "POTENTIAL_ANOMALY" | "INCONCLUSIVE";
export type ReviewStatus = "UNREVIEWED" | "CONFIRMED" | "DISMISSED" | "INCONCLUSIVE";
export type AnalyzerStatus = "AVAILABLE" | "UNAVAILABLE" | "ERROR" | "NOT_APPLICABLE";

export type DocType =
  | "id_card"
  | "application_form"
  | "bank_statement"
  | "other"
  | "UNKNOWN"
  | "AMBIGUOUS";

export type EntityRole =
  | "applicant"
  | "co_applicant"
  | "guarantor"
  | "nominee"
  | "account_holder"
  | "unknown";

export type Severity = "info" | "low" | "medium" | "high";
export type PriorityBand = "low" | "moderate" | "high" | "critical";

/** bbox = [x, y, w, h], each normalized 0..1 against the canonical render. */
export interface Region {
  docId: string;
  page: number; // 1-based
  bbox: [x: number, y: number, w: number, h: number];
  label?: string;
}

export interface PageRecord {
  docId: string;
  page: number; // 1-based
  renderPath: string; // canonical render, derived folder
  renderDpi: number;
  rotationDeg: number; // applied to reach upright
  deskewDeg: number;
  docType: DocType;
  docTypeConfidence: number;
  docTypeOverriddenBy?: string; // analyst id if corrected
}

export interface DocumentRecord {
  docId: string;
  caseId: string;
  filename: string;
  sha256: string;
  mimeType: string;
  pageCount: number;
  processing: Record<string, AnalyzerStatus>;
  pipelineVersion: string;
  receivedAt: string; // ISO timestamp
}

export interface ExtractedField {
  fieldId: string; // unique within its document: (docId, fieldId) identifies a field (the Python service numbers FLD-001... per document)
  docId: string;
  name: string; // "full_name", "dob", "address", ...
  rawValue: string; // exactly as read
  normalizedValue: string | null;
  ocrConfidence: number; // how well the engine read the characters
  extractionConfidence: number; // how sure we are this text is this field
  validationStatus: "VALID" | "INVALID_FORMAT" | "NOT_CHECKED";
  region: Region;
  source: "ocr" | "pdf_text_layer";
}

export interface DocumentAssertion {
  assertionId: string;
  caseId: string;
  docId: string;
  fieldId: string;
  entityId: string | null; // null until resolved
  entityRole: EntityRole;
  semanticType: string; // "dob", "address_permanent", "address_current", "requested_amount", "account_balance", "id_number", ...
  normalizedValue: string | null;
  documentDate?: string; // when the document was issued
  effectiveDate?: string; // when the information applies
  extractionConfidence: number;
  region: Region;
}

export interface Entity {
  entityId: string;
  caseId: string;
  role: EntityRole;
  resolution: "RESOLVED" | "SUGGESTED" | "UNRESOLVED";
  basis: string[]; // e.g. ["id_number exact match: TD-1 (D-01, D-02)"]
  /** Only for resolution SUGGESTED: the entities this one resembles. A suggestion never merges entities. */
  suggestedLinks?: Array<{ entityId: string; score: number; method: "name_token_sort" }>;
  assertionIds: string[];
}

export interface Provenance {
  analyzer: string;
  analyzerVersion: string;
  modelVersion?: string;
  config: Record<string, number | string>; // thresholds used
  inputSha256: string;
  processedAt: string;
  status: AnalyzerStatus;
}

export interface Evidence {
  evidenceId: string;
  kind: "assertion_pair" | "forensic_signal" | "metadata" | "rule_result" | "text_layer_diff";
  description: string;
  regions: Region[];
  assertionIds?: string[];
  measurements?: Record<string, number | string>;
  provenance: Provenance;
}

export interface StructuredExplanation {
  findingId: string;
  summary: string;
  observations: string[];
  possibleExplanations: string[];
  recommendedReview: string[];
  evidenceIds: string[];
}

export interface Finding {
  findingId: string;
  caseId: string;
  category: "extraction" | "visual" | "pdf_structure" | "context" | "cross_document";
  type: string; // "DOB_CONFLICT", "TEXT_LAYER_MISMATCH", "REGION_EVENT", ...
  observationStatus: ObservationStatus; // machine output, never edited
  severity: Severity;
  confidence: number; // kept separate from severity
  evidence: Evidence[];
  explanation?: StructuredExplanation;
  needsVerification: string[];
}

/** Append-only analyst/system log entry. Distinct from RegionEvent (grouped forensic signals). */
export interface InvestigationEvent {
  eventId: string;
  caseId: string;
  at: string;
  actor: string;
  type: "UPLOAD" | "RERUN" | "RECLASSIFY" | "REVIEW" | "COMMENT" | "REOPEN" | "EXPORT" | "VIEW";
  targetId: string; // docId, findingId, ...
  payload: Record<string, unknown>; // e.g. { status: "DISMISSED", reason: "..." }
}

/**
 * One raw observation from a Python analyzer.
 * `type` examples: ELA_ANOMALY, NOISE_INCONSISTENCY, COPY_MOVE, TEXT_LAYER_MISMATCH,
 * FONT_OUTLIER, PDF_METADATA, PDF_REVISIONS, SIGNATURE_DUPLICATE, SIGNATURE_PLACEMENT.
 * `detector` is the analyzer id (e.g. "image.ela"); the engine maps detectors to families via config.
 */
export interface ForensicSignal {
  signalId: string; // unique within its document, like fieldId
  detector: string;
  type: string;
  regions: Region[];
  confidence: number;
  measurements: Record<string, number | string>;
  provenance: Provenance;
}

/** Output of the Python analysis service, POST /analyze. */
export interface AnalyzeResponse {
  docId: string;
  pipelineVersion: string;
  pages: PageRecord[];
  fields: ExtractedField[];
  signals: ForensicSignal[];
  analyzerStatuses: Record<string, AnalyzerStatus>;
  provenance: Provenance[];
}

/** Several signals on overlapping boxes of one page, merged into one event (ARCHITECTURE section 8). */
export interface RegionEvent {
  eventId: string;
  caseId: string;
  docId: string;
  page: number; // lowest page among the member regions; 0 for a document-level signal with no region (regions is then empty)
  regions: Region[]; // merged/union boxes of the member signals
  signalIds: string[];
  detectors: string[];
  confidence: number; // max member confidence plus agreement bonus, capped at 1
  provenance: Provenance;
}

export interface PriorityResult {
  priorityBand: PriorityBand;
  score: number;
  confidence: number; // reported separately from score
  coverage: number; // share of expected analyzers that actually ran
  /** Expected analyzers that did not report for the case (or for the document), so their checks did not run. Absent when none. */
  missingAnalyzers?: string[];
  /** True when coverage is below the configured threshold: the band rests on part of the intended checks. */
  incompleteCoverage?: boolean;
  contributingFindingIds: string[];
}

/** Investigation priority. Never a fraud probability and never called "risk". */
export interface PriorityOutput {
  perCase: PriorityResult;
  perDocument: Record<string, PriorityResult>;
  provenance: Provenance; // echoes all weights and thresholds used
}

export type JobStatus = "QUEUED" | "RUNNING" | "PARTIAL" | "DONE" | "FAILED";
export type AnalyzerRunState = "PENDING" | "RUNNING" | "DONE" | "ERROR" | "UNAVAILABLE" | "NOT_APPLICABLE";

export interface AnalyzerProgress {
  state: AnalyzerRunState;
  analyzerStatus?: AnalyzerStatus; // set once the analyzer has reported
  startedAt?: string;
  finishedAt?: string;
  message?: string;
}

export interface Job {
  jobId: string;
  caseId: string;
  docId: string;
  pipelineVersion: string; // idempotency key is (docId, pipelineVersion)
  status: JobStatus;
  analyzers: Record<string, AnalyzerProgress>; // per-analyzer progress
  attempts: number;
  maxAttempts: number;
  forced?: boolean; // analyst forced a re-run
  createdAt: string;
  updatedAt: string;
  error?: string;
}

/** One row of backend/config/comparability.json (ARCHITECTURE section 7.2). */
export interface ComparabilityRule {
  ruleId: string;
  ruleType: "MUST_AGREE" | "MAY_DIFFER" | "NOT_COMPARABLE" | "ARITHMETIC" | "TEMPORAL" | "UNIQUENESS";
  semanticTypes: string[];
  entityRoles: EntityRole[];
  matchMethod:
    | "exact"
    | "name_fuzzy"
    | "date_iso"
    | "amount_exact"
    | "sum_equals"
    | "date_order"
    | "none";
  severity: Severity;
  note: string;
  /** Finding type produced when the rule fires, e.g. "DOB_CONFLICT". Variants derive from it (NAME_VARIANT_SPELLING, DOB_DAY_MONTH_SWAP). */
  findingType?: string;
}
