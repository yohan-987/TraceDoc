/**
 * B7b: reasoning facade. The single entry point between the backend platform and the reasoning engine.
 *
 *   runReasoning(input: ReasoningInput): ReasoningOutput          (contracts/reasoning.ts)
 *
 * Composition, with no I/O, no clock and no randomness (same input, same output):
 *   buildAssertions -> resolveEntities -> evaluate (comparability + context rules)
 *   -> groupEvents (forensic signals) -> eventToFinding -> computePriority
 *
 * Analyzer statuses: a signal reported by an analyzer whose status is UNAVAILABLE, ERROR or NOT_APPLICABLE is ignored (such an analyzer
 * cannot vouch for its output), and every status feeds priority coverage, so a missing analyzer lowers coverage and is never read as clean.
 * Hashes and timestamps for provenance come from the input documents' own provenance (first inputSha256, latest processedAt).
 */
import type { ReasoningInput, ReasoningOutput } from "../../../contracts/reasoning";
import type { AnalyzeResponse, ForensicSignal } from "../../../contracts/types";
import { buildAssertions } from "./assertions";
import { evaluate, nextIdStart } from "./comparability";
import { resolveEntities } from "./entityResolver";
import { eventToFinding } from "./eventFindings";
import { groupEvents } from "./eventGrouper";
import { computePriority } from "./priorityEngine";
import { DEFAULT_PROCESSED_AT } from "./ruleSupport";

export const ENGINE_VERSION = "0.1.0";

const documentSha = (d: AnalyzeResponse): string | undefined =>
  d.provenance.find((p) => p.inputSha256)?.inputSha256 ?? d.signals.find((s) => s.provenance.inputSha256)?.provenance.inputSha256;

function latestProcessedAt(docs: readonly AnalyzeResponse[]): string {
  const all = docs.flatMap((d) => [...d.provenance.map((p) => p.processedAt), ...d.signals.map((s) => s.provenance.processedAt)]).filter(Boolean);
  return all.length ? all.reduce((a, b) => (a >= b ? a : b)) : DEFAULT_PROCESSED_AT;
}

export function runReasoning(input: ReasoningInput): ReasoningOutput {
  const { caseId, documents } = input;
  const ids = new Set<string>();
  for (const d of documents) {
    if (ids.has(d.docId)) throw new Error(`runReasoning: duplicate document ${d.docId}`);
    ids.add(d.docId);
  }
  const docSha256: Record<string, string> = {};
  for (const d of documents) { const sha = documentSha(d); if (sha) docSha256[d.docId] = sha; }
  const processedAt = latestProcessedAt(documents);
  const opts = { docSha256, processedAt };

  const built = buildAssertions(documents.flatMap((d) => d.fields), documents.flatMap((d) => d.pages), caseId);
  const { entities, assertions } = resolveEntities(built);
  const ruleFindings = evaluate(assertions, entities, opts);

  const usable = documents.map((d) => ({
    docId: d.docId,
    signals: d.signals.filter((s) => d.analyzerStatuses[s.detector] === undefined || d.analyzerStatuses[s.detector] === "AVAILABLE"),
  }));
  const events = groupEvents(usable, caseId, opts);
  // signal ids only have to be unique within their document
  const signalOf = new Map<string, ForensicSignal>(usable.flatMap((u) => u.signals.map((s) => [`${u.docId}\u0000${s.signalId}`, s] as const)));
  const next = nextIdStart(ruleFindings);
  const eventFindings = events.map((ev, i) =>
    eventToFinding(ev, ev.signalIds.flatMap((id) => signalOf.get(`${ev.docId}\u0000${id}`) ?? []), assertions.filter((a) => a.docId === ev.docId), {
      findingNumber: next.finding + i, evidenceNumber: next.evidence + i,
    }));
  const findings = [...ruleFindings, ...eventFindings];

  const priority = computePriority(
    { findings, analyzerStatuses: Object.fromEntries(documents.map((d) => [d.docId, d.analyzerStatuses])) },
    opts,
  );
  return { assertions, entities, findings, events, priority };
}

export { nextIdStart } from "./comparability";
