/**
 * B7: event grouper. Pure, deterministic, never mutates its input.
 *
 * groupEvents(documents, caseId, options) clusters the ForensicSignals of each document into RegionEvents: two signals
 * belong to one event when a region of one overlaps a region of the other on the same page with IoU >= iouThreshold,
 * transitively (A overlaps B, B overlaps C: one event). Signals of different documents are never grouped.
 *
 *  - event confidence = max member confidence + agreementBonus per ADDITIONAL distinct detector family, capped at 1.
 *    Several signals from the same family agree with themselves, not with each other, so they add no bonus.
 *  - event regions: overlapping member regions (IoU >= threshold) are merged into one union box; other regions stay as they are.
 *  - a signal with no region (document level, e.g. PDF metadata) is its own event with page 0 and no regions.
 *  - events are numbered EV-001... in order of their first signal; thresholds live in backend/config/engine.json.
 */
import type { Evidence, ForensicSignal, Provenance, Region, RegionEvent } from "../../../contracts/types";
import engineConfig from "../../config/engine.json";
import { ANALYZER_VERSION, DEFAULT_PROCESSED_AT, inputDigest, pad, roundTo } from "./ruleSupport";

export interface DetectorInfo { family: string; category: "visual" | "pdf_structure"; evidenceKind?: Evidence["kind"] }
export interface EventGrouperConfig { iouThreshold: number; agreementBonus: number; detectors: Readonly<Record<string, DetectorInfo>> }
export const DEFAULT_EVENT_CONFIG: EventGrouperConfig = engineConfig.eventGrouper as EventGrouperConfig;

export interface GroupOptions {
  config?: EventGrouperConfig;
  /** docId -> SHA-256 of the original document (provenance). Missing entries become 64 zeros. */
  docSha256?: Readonly<Record<string, string>>;
  processedAt?: string;
}

export interface DocumentSignals { docId: string; signals: readonly ForensicSignal[] }

type BBox = Region["bbox"];

export function iou(a: BBox, b: BBox): number {
  const iw = Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]);
  const ih = Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]);
  if (iw <= 0 || ih <= 0) return 0;
  const inter = iw * ih;
  const union = a[2] * a[3] + b[2] * b[3] - inter;
  return union > 0 ? inter / union : 0;
}

const r4 = (v: number): number => roundTo(v, 4);

function unionBox(a: BBox, b: BBox): BBox {
  const x0 = Math.min(a[0], b[0]), y0 = Math.min(a[1], b[1]);
  const x1 = Math.max(a[0] + a[2], b[0] + b[2]), y1 = Math.max(a[1] + a[3], b[1] + b[3]);
  return [r4(x0), r4(y0), r4(x1 - x0), r4(y1 - y0)];
}

/** Merges member regions that overlap (same page, IoU >= threshold) into union boxes; others are copied unchanged. */
function mergeRegions(regions: readonly Region[], threshold: number): Region[] {
  const out: Region[] = regions.map((r) => ({ ...r, bbox: [...r.bbox] as BBox }));
  let merged = true;
  while (merged) {
    merged = false;
    outer: for (let i = 0; i < out.length; i++) for (let j = i + 1; j < out.length; j++) {
      const a = out[i], b = out[j];
      if (!a || !b || a.docId !== b.docId || a.page !== b.page || iou(a.bbox, b.bbox) < threshold) continue;
      out[i] = { ...a, bbox: unionBox(a.bbox, b.bbox) };
      out.splice(j, 1);
      merged = true;
      break outer;
    }
  }
  return out;
}

const overlaps = (x: ForensicSignal, y: ForensicSignal, threshold: number): boolean =>
  x.regions.some((a) => y.regions.some((b) => a.page === b.page && iou(a.bbox, b.bbox) >= threshold));

/** Category for an event, from its detectors: visual if any detector is visual (or unknown), pdf_structure if all are PDF structure. */
export function categoryOfDetectors(detectors: readonly string[], config: EventGrouperConfig = DEFAULT_EVENT_CONFIG): "visual" | "pdf_structure" {
  return detectors.length > 0 && detectors.every((d) => config.detectors[d]?.category === "pdf_structure") ? "pdf_structure" : "visual";
}

export function groupEvents(documents: readonly DocumentSignals[], caseId: string, options: GroupOptions = {}): RegionEvent[] {
  const cfg = options.config ?? DEFAULT_EVENT_CONFIG;
  const events: RegionEvent[] = [];
  for (const doc of documents) {
    const seen = new Set<string>(); // signal ids only have to be unique within their document
    const sigs = doc.signals;
    for (const s of sigs) {
      if (seen.has(s.signalId)) throw new Error(`groupEvents: duplicate signalId ${s.signalId} in document ${doc.docId}`);
      seen.add(s.signalId);
    }
    const parent = sigs.map((_, i) => i);
    const find = (i: number): number => { let r = i; while ((parent[r] ?? r) !== r) r = parent[r] ?? r; return r; };
    for (let i = 0; i < sigs.length; i++) for (let j = i + 1; j < sigs.length; j++) {
      const a = sigs[i], b = sigs[j];
      if (!a || !b || !overlaps(a, b, cfg.iouThreshold)) continue;
      const ri = find(i), rj = find(j);
      if (ri !== rj) parent[Math.max(ri, rj)] = Math.min(ri, rj);
    }
    const clusters = new Map<number, ForensicSignal[]>();
    sigs.forEach((s, i) => { const r = find(i); clusters.set(r, [...(clusters.get(r) ?? []), s]); });
    for (const members of [...clusters.entries()].sort((a, b) => a[0] - b[0]).map((e) => e[1])) {
      const detectors = [...new Set(members.map((m) => m.detector))];
      const families = new Set(detectors.map((d) => cfg.detectors[d]?.family ?? d));
      const base = Math.max(...members.map((m) => m.confidence));
      const regions = mergeRegions(members.flatMap((m) => m.regions), cfg.iouThreshold);
      const provenance: Provenance = {
        analyzer: "eventGrouper", analyzerVersion: ANALYZER_VERSION, config: { iouThreshold: cfg.iouThreshold, agreementBonus: cfg.agreementBonus },
        inputSha256: inputDigest([doc.docId], options.docSha256 ?? {}), processedAt: options.processedAt ?? DEFAULT_PROCESSED_AT, status: "AVAILABLE",
      };
      events.push({
        eventId: `EV-${pad(events.length + 1, 3)}`, caseId, docId: doc.docId,
        page: regions.length ? Math.min(...regions.map((r) => r.page)) : 0,
        regions, signalIds: members.map((m) => m.signalId), detectors,
        confidence: roundTo(Math.min(1, base + cfg.agreementBonus * (families.size - 1)), 2), provenance,
      });
    }
  }
  return events;
}
