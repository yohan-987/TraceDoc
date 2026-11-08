/**
 * B7: priority engine. Pure, deterministic, never mutates its input.
 *
 * Produces an INVESTIGATION PRIORITY, never a fraud probability. It says how much attention a case or document deserves,
 * given the findings that were produced; it says nothing about intent.
 *
 * Per finding: term = severityWeight * confidence (severity and confidence stay separate inputs).
 * Per category: noisy-OR of the terms. A category whose findings are all info/low is capped at weakEvidenceCap (moderate band).
 * Across categories: noisy-OR of categoryWeight * categoryScore. If no finding is medium or high, the final score is capped at
 * weakEvidenceCap as well, so many weak signals can never reach the high bands.
 * Coverage = AVAILABLE / (AVAILABLE + UNAVAILABLE + ERROR) analyzers (NOT_APPLICABLE excluded). It is reported next to the
 * score and never changes it: a missing analyzer contributes nothing, lowers coverage, and does not lower the band.
 * An expected analyzer (config priority.expectedAnalyzers) that a document does not report at all counts as UNAVAILABLE:
 * a check that was never built or never ran is missing, not clean. The exception is a family (the first dotted segment, e.g.
 * "pdf") for which every reported member is NOT_APPLICABLE on that document: the whole family does not apply there.
 * Below lowCoverageBelow the result is flagged incompleteCoverage and lists missingAnalyzers, so "low" is never shown as a
 * plain all-clear.
 * Confidence = severity-weighted mean of the contributing findings' confidences (0 when there are none).
 * Per document: the findings whose evidence touches that document. Per case: all findings.
 */
import type { AnalyzerStatus, Finding, PriorityBand, PriorityOutput, PriorityResult, Provenance, Severity } from "../../../contracts/types";
import engineConfig from "../../config/engine.json";
import { ANALYZER_VERSION, DEFAULT_PROCESSED_AT, inputDigest, roundTo } from "./ruleSupport";

export interface PriorityConfig {
  severityWeights: Readonly<Record<Severity, number>>;
  categoryWeights: Readonly<Record<Finding["category"], number>>;
  bands: { lowBelow: number; moderateBelow: number; highBelow: number };
  weakEvidenceCap: number;
  /** Analyzer ids the system is meant to run; one that a document does not report counts as UNAVAILABLE. */
  expectedAnalyzers: readonly string[];
  /** Coverage below this marks the result incompleteCoverage. */
  lowCoverageBelow: number;
}

export class PriorityConfigError extends Error {}

export function validatePriorityConfig(cfg: PriorityConfig): PriorityConfig {
  const ids = cfg.expectedAnalyzers as unknown;
  if (!Array.isArray(ids) || ids.length === 0) throw new PriorityConfigError("priority.expectedAnalyzers must be a non-empty list of analyzer ids");
  if (ids.some((id) => typeof id !== "string" || id.trim() === "" || id !== id.trim())) {
    throw new PriorityConfigError("priority.expectedAnalyzers must contain only non-empty analyzer ids without surrounding spaces");
  }
  if (new Set(ids).size !== ids.length) throw new PriorityConfigError("priority.expectedAnalyzers must not contain duplicates");
  if (typeof cfg.lowCoverageBelow !== "number" || !Number.isFinite(cfg.lowCoverageBelow) || cfg.lowCoverageBelow <= 0 || cfg.lowCoverageBelow > 1) {
    throw new PriorityConfigError("priority.lowCoverageBelow must be a number in (0, 1]");
  }
  return cfg;
}
export const DEFAULT_PRIORITY_CONFIG: PriorityConfig = validatePriorityConfig(engineConfig.priority as PriorityConfig);

export interface PriorityInput {
  findings: readonly Finding[];
  /** docId -> analyzer -> status, for every document of the case (documents without findings still get a result). */
  analyzerStatuses: Readonly<Record<string, Readonly<Record<string, AnalyzerStatus>>>>;
}
export interface PriorityOptions {
  config?: PriorityConfig;
  docSha256?: Readonly<Record<string, string>>;
  processedAt?: string;
}

export function bandOf(score: number, bands: PriorityConfig["bands"]): PriorityBand {
  if (score < bands.lowBelow) return "low";
  if (score < bands.moderateBelow) return "moderate";
  if (score < bands.highBelow) return "high";
  return "critical";
}

function resultFor(findings: readonly Finding[], statuses: readonly AnalyzerStatus[], missing: readonly string[], cfg: PriorityConfig): PriorityResult {
  const byCategory = new Map<Finding["category"], Finding[]>();
  for (const f of findings) byCategory.set(f.category, [...(byCategory.get(f.category) ?? []), f]);
  let combined = 1;
  for (const [category, fs] of byCategory) {
    let notHit = 1;
    for (const f of fs) notHit *= 1 - cfg.severityWeights[f.severity] * f.confidence;
    let score = 1 - notHit;
    if (fs.every((f) => f.severity === "info" || f.severity === "low")) score = Math.min(score, cfg.weakEvidenceCap);
    combined *= 1 - (cfg.categoryWeights[category] ?? 0) * score;
  }
  let score = findings.length ? 1 - combined : 0;
  if (findings.every((f) => f.severity === "info" || f.severity === "low")) score = Math.min(score, cfg.weakEvidenceCap);
  score = roundTo(score, 3);
  const weight = findings.reduce((s, f) => s + cfg.severityWeights[f.severity], 0);
  const confidence = findings.length ? roundTo(findings.reduce((s, f) => s + cfg.severityWeights[f.severity] * f.confidence, 0) / weight, 2) : 0;
  const ran = statuses.filter((s) => s === "AVAILABLE").length;
  const expected = statuses.filter((s) => s === "AVAILABLE" || s === "UNAVAILABLE" || s === "ERROR").length;
  const coverage = expected ? roundTo(ran / expected, 3) : 0;
  return {
    priorityBand: bandOf(score, cfg.bands), score, confidence, coverage,
    contributingFindingIds: findings.map((f) => f.findingId),
    ...(missing.length ? { missingAnalyzers: [...missing] } : {}),
    ...(coverage < cfg.lowCoverageBelow ? { incompleteCoverage: true } : {}),
  };
}

const familyOf = (id: string): string => id.split(".")[0] ?? id;

/** Reported statuses plus one UNAVAILABLE for every expected analyzer the document did not report (see header). `missing` lists the expected
 * analyzers whose checks did not run: not reported, UNAVAILABLE or ERROR. */
export function effectiveStatuses(reported: Readonly<Record<string, AnalyzerStatus>>, expected: readonly string[]): { statuses: AnalyzerStatus[]; missing: string[] } {
  const statuses: AnalyzerStatus[] = Object.values(reported);
  const missing: string[] = [];
  for (const id of expected) {
    if (id in reported) {
      if (reported[id] === "UNAVAILABLE" || reported[id] === "ERROR") missing.push(id); // reported, but its results are missing
      continue;
    }
    const siblings = Object.entries(reported).filter(([other]) => familyOf(other) === familyOf(id)).map(([, s]) => s);
    if (siblings.length > 0 && siblings.every((s) => s === "NOT_APPLICABLE")) { statuses.push("NOT_APPLICABLE"); continue; }
    statuses.push("UNAVAILABLE");
    missing.push(id);
  }
  return { statuses, missing: missing.sort() };
}

const findingDocs = (f: Finding): Set<string> => new Set(f.evidence.flatMap((e) => e.regions.map((r) => r.docId)));

export function configEcho(cfg: PriorityConfig): Record<string, number | string> {
  return {
    ...Object.fromEntries(Object.entries(cfg.severityWeights).map(([k, v]) => [`severityWeight_${k}`, v])),
    ...Object.fromEntries(Object.entries(cfg.categoryWeights).map(([k, v]) => [`categoryWeight_${k}`, v])),
    bandLowBelow: cfg.bands.lowBelow, bandModerateBelow: cfg.bands.moderateBelow, bandHighBelow: cfg.bands.highBelow, weakEvidenceCap: cfg.weakEvidenceCap,
    lowCoverageBelow: cfg.lowCoverageBelow, expectedAnalyzers: cfg.expectedAnalyzers.join(","),
  };
}

export function computePriority(input: PriorityInput, options: PriorityOptions = {}): PriorityOutput {
  const cfg = options.config ?? DEFAULT_PRIORITY_CONFIG;
  const docIds = Object.keys(input.analyzerStatuses);
  const perDocument: Record<string, PriorityResult> = {};
  const allStatuses: AnalyzerStatus[] = [];
  const allMissing = new Set<string>();
  for (const docId of docIds) {
    const { statuses, missing } = effectiveStatuses(input.analyzerStatuses[docId] ?? {}, cfg.expectedAnalyzers);
    perDocument[docId] = resultFor(input.findings.filter((f) => findingDocs(f).has(docId)), statuses, missing, cfg);
    allStatuses.push(...statuses);
    missing.forEach((m) => allMissing.add(m));
  }
  const provenance: Provenance = {
    analyzer: "priorityEngine", analyzerVersion: ANALYZER_VERSION, config: configEcho(cfg), inputSha256: inputDigest(docIds, options.docSha256 ?? {}),
    processedAt: options.processedAt ?? DEFAULT_PROCESSED_AT, status: "AVAILABLE",
  };
  return { perCase: resultFor(input.findings, allStatuses, [...allMissing].sort(), cfg), perDocument, provenance };
}
