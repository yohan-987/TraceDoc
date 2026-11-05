import type { Case, DossierJson, FindingWithReview } from "@contracts/api";
import type {
  AnalyzeResponse, DocumentRecord, ForensicSignal, InvestigationEvent, Job, RegionEvent,
} from "@contracts/types";
import type { EvidenceRepository } from "../repo/evidence.js";
import type { DocumentRepository } from "../repo/repositories.js";
import type { OverlayAnalysisRepository } from "../repo/overrides.js";
import { jobNumber } from "../repo/repositories.js";
import type { CaseService } from "../services/cases.js";
import type { JsonlEventLog } from "../services/eventLog.js";
import type { IntegrityService } from "../services/integrity.js";
import type { JobService } from "../services/jobs.js";
import type { ReviewSource } from "../services/reviewSource.js";

/** Everything the dossier shows: the contract's DossierJson plus the extra detail only the PDF prints. */
export interface DossierBundle {
  json: DossierJson;
  caseRecord: Case;
  generation?: number;
  documents: DocumentRecord[];
  analyses: Record<string, AnalyzeResponse | undefined>;
  jobs: Record<string, Job | undefined>; // latest job per document
  uploads: Record<string, InvestigationEvent | undefined>; // the UPLOAD event per document
  regionEvents: RegionEvent[];
  signals: ForensicSignal[];
}

/** Analyst decisions, in order. VIEW, UPLOAD, RERUN and EXPORT are in the investigation log but are not decisions. */
const DECISION_TYPES = new Set<InvestigationEvent["type"]>(["REVIEW", "COMMENT", "REOPEN", "RECLASSIFY"]);

export interface BundleDeps {
  cases: CaseService;
  documents: DocumentRepository;
  jobs: JobService;
  analysis: OverlayAnalysisRepository;
  evidence: EvidenceRepository;
  reviews: ReviewSource;
  events: JsonlEventLog;
  integrity: IntegrityService;
  now?: () => Date;
}

export class DossierBuilder {
  private readonly now: () => Date;
  constructor(private readonly d: BundleDeps) {
    this.now = d.now ?? (() => new Date());
  }

  async build(caseId: string): Promise<DossierBundle> {
    const caseRecord = await this.d.cases.require(caseId);
    const summary = await this.d.cases.summary(caseRecord);
    const documents = await this.d.documents.listByCase(caseId);
    const record = await this.d.evidence.latest(caseId);
    const events = await this.d.events.forCase(caseId);
    const allJobs = await this.d.jobs.list();

    const jobs: DossierBundle["jobs"] = {};
    const analyses: DossierBundle["analyses"] = {};
    const uploads: DossierBundle["uploads"] = {};
    for (const doc of documents) {
      jobs[doc.docId] = allJobs.filter((j) => j.docId === doc.docId).sort((a, b) => jobNumber(a.jobId) - jobNumber(b.jobId)).at(-1);
      analyses[doc.docId] = await this.d.analysis.get(doc.docId);
      uploads[doc.docId] = events.find((e) => e.type === "UPLOAD" && e.targetId === doc.docId);
    }

    const findings: FindingWithReview[] = [];
    for (const finding of record?.findings ?? []) {
      findings.push({ finding, reviewStatus: await this.d.reviews.statusOf(finding.findingId) });
    }
    const analyzerStatuses: DossierJson["analyzerStatuses"] = {};
    for (const doc of documents) analyzerStatuses[doc.docId] = { ...(analyses[doc.docId]?.analyzerStatuses ?? doc.processing) };

    const integrity = await this.d.integrity.check(caseId);
    const json: DossierJson = {
      caseId,
      generatedAt: this.now().toISOString(),
      summary,
      assertions: record?.assertions ?? [],
      entities: record?.entities ?? [],
      findings,
      reviewHistory: events.filter((e) => DECISION_TYPES.has(e.type)),
      analyzerStatuses,
      limitations: [],
      integrity,
    };
    const bundle: DossierBundle = {
      json, caseRecord, documents, analyses, jobs, uploads,
      ...(record ? { generation: record.generation } : {}),
      regionEvents: record?.events ?? [],
      signals: record?.signals ?? [],
    };
    json.limitations = computeLimitations(bundle, record !== undefined);
    return bundle;
  }
}

/**
 * The gaps a reader must know about. Each one is something that is missing, not something that came out clean:
 * failed or unfinished analysis, analyzers that did not run, no reasoning run, partial coverage, integrity failures.
 */
export function computeLimitations(b: DossierBundle, reasoningRan: boolean): string[] {
  const out: string[] = [];
  const name = (d: DocumentRecord) => `${d.docId} (${d.filename})`;

  for (const doc of b.documents) {
    const job = b.jobs[doc.docId];
    if (!job) out.push(`${name(doc)}: no analysis job exists, so nothing was extracted from this document.`);
    else if (job.status === "QUEUED" || job.status === "RUNNING") out.push(`${name(doc)}: analysis had not finished when this dossier was generated.`);
    else if (job.status === "FAILED") out.push(`${name(doc)}: analysis failed${job.error ? ` (${job.error})` : ""}. No fields or forensic results exist for it, which is not the same as a clean result.`);

    for (const [id, status] of Object.entries(b.json.analyzerStatuses[doc.docId] ?? {})) {
      if (status !== "ERROR" && status !== "UNAVAILABLE") continue;
      const message = job?.analyzers[id]?.message;
      out.push(`${name(doc)}: analyzer ${id} was ${status}${message ? ` (${message})` : ""}. Its results are missing, not clean.`);
    }
  }
  if (b.documents.length === 0) out.push("The case has no documents.");
  else if (!reasoningRan) out.push("The reasoning engine has not run for this case, so there are no findings. That does not mean there are no inconsistencies.");

  const cov = b.json.summary.priority?.coverage;
  if (cov !== undefined && cov < 1) out.push(`Analysis coverage is ${cov}: some analyzers did not run, so the priority is based on part of the available checks.`);

  for (const d of b.json.integrity.documents) {
    if (d.status === "FAIL") {
      const detail = d.actualSha256 === null ? "the stored file is missing or unreadable" : d.expectedSha256 === "" ? "no recorded hash exists for it" : "its hash differs from the one recorded at upload";
      out.push(`${d.docId}: integrity check FAILED (${detail}). Treat results for this document with caution.`);
    }
  }
  return out;
}
