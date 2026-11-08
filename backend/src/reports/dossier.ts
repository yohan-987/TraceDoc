import PDFDocument from "pdfkit";
import type { FindingWithReview } from "@contracts/api";
import type { AnalyzerStatus, Evidence, Finding, InvestigationEvent, ObservationStatus, Region, ReviewStatus } from "@contracts/types";
import rulesConfig from "../../config/comparability.json";
import type { DossierBundle } from "./bundle.js";

type Pdf = InstanceType<typeof PDFDocument>;

/**
 * pdfkit's openImage() decodes an image once so it can be drawn many times (each crop of the same page reuses one embedded
 * copy). It exists at runtime but @types/pdfkit does not declare it, so it is typed here.
 */
interface PdfImage { width: number; height: number }
const openImage = (doc: Pdf, buf: Buffer): PdfImage => (doc as unknown as { openImage(src: Buffer): PdfImage }).openImage(buf);
const drawImage = (doc: Pdf, img: PdfImage, x: number, y: number, width: number): void =>
  void (doc as unknown as { image(src: PdfImage, x: number, y: number, o: { width: number }): unknown }).image(img, x, y, { width });

// ----------------------------------------------------------------------------------------------- wording
/**
 * Findings are observations or potential anomalies, never conclusions about intent. The machine's observation
 * and the analyst's review are always shown side by side, because a review never changes what the machine observed.
 */
export const OBSERVATION_LABEL: Record<ObservationStatus, string> = {
  OBSERVED: "Observed",
  INCONSISTENT: "Inconsistency observed in extracted values",
  POTENTIAL_ANOMALY: "Potential anomaly (needs verification)",
  INCONCLUSIVE: "Inconclusive",
};

export const REVIEW_LABEL: Record<ReviewStatus, string> = {
  UNREVIEWED: "Not yet reviewed",
  CONFIRMED: "Confirmed by analyst",
  DISMISSED: "Dismissed by analyst",
  INCONCLUSIVE: "Marked inconclusive by analyst",
};

/** Possible benign explanations, by kind of finding. Printed as prompts for verification, clearly marked as general. */
const GENERAL_CONSIDERATIONS: Record<Finding["category"], string[]> = {
  cross_document: [
    "A transcription or data-entry difference on one of the documents.",
    "An OCR misread of one value; compare the region crops with the original.",
    "A genuine change over time, for example a name or an address that was updated.",
    "The two values may belong to different people (check how the entities were resolved).",
  ],
  context: [
    "A row that was missed or misread by the extractor.",
    "A printed total that includes items not listed on the page that was analysed.",
    "A rounding or formatting difference in how amounts were written.",
  ],
  extraction: ["A field that was hard to read; compare the region crop with the original."],
  visual: [
    "Scanning, compression or resizing artefacts that make one region look different from its surroundings.",
    "A legitimate correction, stamp, signature or annotation applied to the document.",
    "A region with different content density (for example digits) than the text around it.",
  ],
  pdf_structure: [
    "A PDF that was re-exported, optimised or merged by ordinary software.",
    "A text layer added later by an OCR tool, which can differ from the rendered page.",
    "A font substitution made by the software that produced the PDF.",
  ],
};

const RULE_NOTES = new Map((rulesConfig as { ruleId: string; note?: string }[]).map((r) => [r.ruleId, r.note ?? ""]));

const CATEGORY_LABEL: Record<Finding["category"], string> = {
  cross_document: "Cross-document", context: "Context rules", extraction: "Extraction", visual: "Visual forensic", pdf_structure: "PDF structure",
};

// ----------------------------------------------------------------------------------------------- text safety
/** The built-in PDF fonts cover Latin-1 and a few typographic marks; anything else is shown as "?" instead of corrupting the page. */
const CP1252_EXTRA = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ";
export function safe(input: unknown): string {
  const s = String(input ?? "");
  let out = "";
  for (const ch of s) {
    const c = ch.codePointAt(0)!;
    if (c === 0x0a) out += "\n";
    else if (c < 0x20 || c === 0x7f) out += " ";
    else if (c <= 0xff || CP1252_EXTRA.includes(ch)) out += ch;
    else out += "?";
  }
  return out;
}

const pct = (n: number) => n.toFixed(2);
const bbox = (b: [number, number, number, number]) => `x ${b[0].toFixed(3)}, y ${b[1].toFixed(3)}, w ${b[2].toFixed(3)}, h ${b[3].toFixed(3)}`;
const measurement = (m?: Record<string, string | number>) =>
  m ? Object.entries(m).map(([k, v]) => `${k}: ${typeof v === "number" ? +v.toFixed(4) : v}`).join("; ") : "";

// ----------------------------------------------------------------------------------------------- region crops
export interface CropGeometry {
  /** The part of the page that is shown, normalized. */
  window: { x: number; y: number; w: number; h: number };
  /** Size of the frame in points. */
  frame: { w: number; h: number };
  /** Points per source pixel. */
  scale: number;
  /** The region's box inside the frame, in points. */
  box: { x: number; y: number; w: number; h: number };
}

/**
 * Which part of a page to show for a region: the region plus some surrounding context (at least `MIN_*` of the page),
 * scaled to fit the frame. Pure, so it can be tested without drawing anything.
 */
export function cropGeometry(region: [number, number, number, number], img: { w: number; h: number }, frameW: number, maxH: number): CropGeometry {
  const [rx, ry, rw, rh] = region;
  const margin = 0.05; // of the page width, on every side
  const marginY = (margin * img.w) / img.h;
  const ww = Math.min(1, Math.max(rw + 2 * margin, 0.4));
  const wh = Math.min(1, Math.max(rh + 2 * marginY, 0.1));
  const cx = rx + rw / 2;
  const cy = ry + rh / 2;
  const wx = Math.min(Math.max(cx - ww / 2, 0), 1 - ww);
  const wy = Math.min(Math.max(cy - wh / 2, 0), 1 - wh);
  const scale = Math.min(frameW / (ww * img.w), maxH / (wh * img.h));
  const frame = { w: ww * img.w * scale, h: wh * img.h * scale };
  return {
    window: { x: wx, y: wy, w: ww, h: wh },
    frame,
    scale,
    box: { x: (rx - wx) * img.w * scale, y: (ry - wy) * img.h * scale, w: rw * img.w * scale, h: rh * img.h * scale },
  };
}

export function pngSize(buf: Buffer): { w: number; h: number } | undefined {
  const sig = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  if (buf.length < 24 || !sig.every((b, i) => buf[i] === b)) return undefined;
  return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}

// ----------------------------------------------------------------------------------------------- layout helpers
const PAGE_W = 595.28;
const PAGE_H = 841.89;
const M = 48;
const W = PAGE_W - 2 * M;
const BOTTOM = PAGE_H - 56;
const C = { text: "#1a1a1a", muted: "#666666", rule: "#cfcfcf", accent: "#1f3a6e", box: "#d62828", band: "#eef1f6", warn: "#8a4b00" };

function ensure(doc: Pdf, h: number): void {
  if (doc.y + h > BOTTOM) doc.addPage();
}

function heading(doc: Pdf, text: string, level: 1 | 2 | 3 = 1): void {
  const size = level === 1 ? 17 : level === 2 ? 12.5 : 10.5;
  ensure(doc, size + 24);
  if (level === 1) doc.moveDown(0.2);
  doc.fillColor(C.accent).font("Helvetica-Bold").fontSize(size).text(safe(text), M, doc.y, { width: W });
  if (level === 1) {
    doc.moveTo(M, doc.y + 2).lineTo(M + W, doc.y + 2).strokeColor(C.accent).lineWidth(1).stroke();
    doc.moveDown(0.5);
  } else doc.moveDown(0.25);
  doc.fillColor(C.text).font("Helvetica").fontSize(9);
}

function para(doc: Pdf, text: string, opts: { muted?: boolean; bold?: boolean; indent?: number; size?: number } = {}): void {
  const size = opts.size ?? 9;
  const x = M + (opts.indent ?? 0);
  const width = W - (opts.indent ?? 0);
  doc.font(opts.bold ? "Helvetica-Bold" : "Helvetica").fontSize(size);
  ensure(doc, doc.heightOfString(safe(text), { width }) + 4);
  doc.fillColor(opts.muted ? C.muted : C.text).text(safe(text), x, doc.y, { width });
  doc.moveDown(0.3);
  doc.fillColor(C.text).font("Helvetica").fontSize(9);
}

function bullets(doc: Pdf, items: string[], indent = 8): void {
  for (const it of items) para(doc, `-  ${it}`, { indent });
}

interface Col { label: string; w: number }
function table(doc: Pdf, cols: Col[], rows: string[][], size = 7.5): void {
  const total = cols.reduce((n, c) => n + c.w, 0);
  const drawHeader = () => {
    const y = doc.y;
    doc.font("Helvetica-Bold").fontSize(size);
    // header height follows the longest label, so a label that wraps never runs into the first row
    const h = Math.max(...cols.map((c) => doc.heightOfString(safe(c.label), { width: c.w - 6 })), 8) + 6;
    doc.rect(M, y, total, h).fill(C.band);
    doc.fillColor(C.accent);
    let x = M;
    for (const c of cols) { doc.text(safe(c.label), x + 3, y + 3, { width: c.w - 6 }); x += c.w; }
    doc.y = y + h + 2;
    doc.fillColor(C.text).font("Helvetica").fontSize(size);
  };
  ensure(doc, 40);
  drawHeader();
  for (const row of rows) {
    doc.font("Helvetica").fontSize(size);
    const heights = row.map((cell, i) => doc.heightOfString(safe(cell), { width: cols[i]!.w - 6 }));
    const h = Math.max(...heights, 8) + 6;
    if (doc.y + h > BOTTOM) { doc.addPage(); drawHeader(); }
    const y = doc.y;
    let x = M;
    row.forEach((cell, i) => { doc.fillColor(C.text).text(safe(cell), x + 3, y + 3, { width: cols[i]!.w - 6 }); x += cols[i]!.w; });
    doc.moveTo(M, y + h).lineTo(M + total, y + h).strokeColor(C.rule).lineWidth(0.5).stroke();
    doc.y = y + h;
  }
  doc.x = M;
  doc.moveDown(0.6);
  doc.fillColor(C.text).font("Helvetica").fontSize(9);
}

// ----------------------------------------------------------------------------------------------- the dossier
export interface RenderOptions {
  /** Canonical page renders, keyed `${docId}#${page}`. A missing page is reported in the dossier, not skipped silently. */
  pageImages: Map<string, Buffer>;
  /** Content-stream compression (on in production; tests turn it off to read the text). */
  compress?: boolean;
}

export function renderDossierPdf(b: DossierBundle, opts: RenderOptions): Promise<Buffer> {
  const doc = new PDFDocument({
    size: "A4", margins: { top: M, bottom: 56, left: M, right: M }, bufferPages: true, compress: opts.compress ?? true,
    info: { Title: `TraceDocs dossier ${b.json.caseId}`, Author: "TraceDocs", Subject: "Investigation dossier", CreationDate: new Date(b.json.generatedAt) },
  });
  const chunks: Buffer[] = [];
  doc.on("data", (c: Buffer) => chunks.push(c));
  const done = new Promise<Buffer>((resolve, reject) => {
    doc.on("end", () => resolve(Buffer.concat(chunks)));
    doc.on("error", reject);
  });

  const images = new Map<string, PdfImage | null>();
  const imageFor = (docId: string, page: number) => {
    const key = `${docId}#${page}`;
    if (!images.has(key)) {
      const buf = opts.pageImages.get(key);
      const size = buf ? pngSize(buf) : undefined;
      images.set(key, buf && size ? openImage(doc, buf) : null);
    }
    return images.get(key) ?? null;
  };

  cover(doc, b);
  section1Overview(doc, b);
  doc.addPage();
  section2Extracted(doc, b);
  doc.addPage();
  section3Context(doc, b);
  doc.addPage();
  section4Visual(doc, b);
  doc.addPage();
  section5Evidence(doc, b, imageFor, opts.pageImages);
  doc.addPage();
  section6Summary(doc, b);
  footers(doc, b);
  doc.end();
  return done;
}

function cover(doc: Pdf, b: DossierBundle): void {
  const j = b.json;
  doc.fillColor(C.accent).font("Helvetica-Bold").fontSize(22).text("TraceDocs Investigation Dossier", M, M, { width: W });
  doc.moveDown(0.3).fillColor(C.text).font("Helvetica").fontSize(11).text(safe(`${j.caseId}: ${b.caseRecord.title}`), { width: W });
  doc.moveDown(0.2).fontSize(9).fillColor(C.muted).text(safe(`Generated ${j.generatedAt}${b.generation ? `   Reasoning run (generation) ${b.generation}` : "   No reasoning run yet"}`), { width: W });
  doc.moveDown(0.8);
  const p = j.summary.priority;
  para(doc, p
    ? `Priority: ${p.priorityBand}   (score ${pct(p.score)}, confidence ${pct(p.confidence)}, coverage ${pct(p.coverage)})`
    : "Priority: not available (no reasoning run for this case)", { bold: true, size: 10.5 });
  if (p?.incompleteCoverage) {
    para(doc, safe(`Incomplete coverage: ${p.missingAnalyzers?.length ?? 0} expected check(s) did not run${p.missingAnalyzers?.length ? ` (${p.missingAnalyzers.join(", ")})` : ""}. This priority rests on part of the intended checks; a low band here does not mean the missing checks found nothing.`), { bold: true, size: 9.5 });
  }
  para(doc, "Priority orders cases for human review. It is not a probability and not a conclusion. Confidence and coverage are reported separately: low coverage means some checks did not run.", { muted: true });
  para(doc, "How to read this document. Every finding is an observation or a potential anomaly in the documents as analysed. None of them states or implies why a difference exists or who is responsible. The machine's observation and the analyst's review are shown side by side, and a review never changes the observation. OCR output is a claim, not ground truth.", { muted: true });
  doc.moveDown(0.4);
}

// 1. Document overview ------------------------------------------------------------------------------
function section1Overview(doc: Pdf, b: DossierBundle): void {
  heading(doc, "1. Document overview");
  para(doc, "Source, type, SHA-256 hash and processing status of every document in the case. The hash is recorded at upload and re-checked in section 6.", { muted: true });
  if (b.documents.length === 0) return para(doc, "This case has no documents.");
  for (const d of b.documents) {
    const analysis = b.analyses[d.docId];
    const job = b.jobs[d.docId];
    const types = [...new Set(analysis?.pages.map((p) => p.docType) ?? [])].join(", ") || "not determined (analysis did not complete)";
    const statuses = Object.values(b.json.analyzerStatuses[d.docId] ?? {});
    const count = (s: AnalyzerStatus) => statuses.filter((x) => x === s).length;
    const upload = b.uploads[d.docId];
    heading(doc, `${d.docId}   ${d.filename}`, 2);
    table(doc, [{ label: "Item", w: 110 }, { label: "Value", w: W - 110 }], [
      ["Document type", types],
      ["Source", upload ? `Uploaded by ${upload.actor} on ${d.receivedAt}` : `Uploaded on ${d.receivedAt}`],
      ["File", `${d.mimeType}, ${d.pageCount} page(s)`],
      ["SHA-256", d.sha256],
      ["Processing status", `${job?.status ?? "no job"}${job && job.attempts > 1 ? ` after ${job.attempts} attempts` : ""}; pipeline ${d.pipelineVersion}`],
      ["Analyzers", statuses.length ? `${count("AVAILABLE")} ran, ${count("NOT_APPLICABLE")} not applicable, ${count("UNAVAILABLE")} unavailable, ${count("ERROR")} failed` : "none reported"],
    ]);
  }
}

// 2. Extracted information --------------------------------------------------------------------------
function section2Extracted(doc: Pdf, b: DossierBundle): void {
  heading(doc, "2. Extracted information");
  para(doc, "What the extractors read from each page, with the confidence of the OCR and of the extraction kept separate. These values are claims made by software. Compare them with the original using the region crops in section 5.", { muted: true });
  para(doc, "OCR is the confidence of the character recognition (1.00 for text read directly from a PDF text layer). Extract is the confidence that the value belongs to this field. Both range from 0 to 1 and are kept separate.", { muted: true, size: 7.5 });
  for (const d of b.documents) {
    const analysis = b.analyses[d.docId];
    heading(doc, `${d.docId}   ${d.filename}`, 2);
    const reclass = b.json.reviewHistory.filter((e) => e.type === "RECLASSIFY" && e.targetId === d.docId);
    for (const e of reclass) para(doc, `Page ${String(e.payload["page"])} was reclassified by ${e.actor} from ${String(e.payload["from"])} to ${String(e.payload["to"])}${e.payload["reason"] ? ` (${String(e.payload["reason"])})` : ""}. The analyzers' own classification was ${String(e.payload["machineDocType"])}.`, { muted: true });
    if (!analysis || analysis.fields.length === 0) {
      para(doc, analysis ? "No fields were extracted from this document." : "No extraction results exist: the analysis did not complete.");
      continue;
    }
    table(doc, [
      { label: "Field", w: 86 }, { label: "Value as read", w: 108 }, { label: "Normalized", w: 96 },
      { label: "OCR", w: 38 }, { label: "Extract", w: 40 }, { label: "Validation", w: 68 }, { label: "Page / source", w: 63 },
    ], analysis.fields.map((f) => [
      f.name, f.rawValue, f.normalizedValue ?? "", pct(f.ocrConfidence), pct(f.extractionConfidence), f.validationStatus,
      `p${f.region.page} ${f.source === "pdf_text_layer" ? "text layer" : "OCR"}`,
    ]));
  }
}

// findings blocks ---------------------------------------------------------------------------------
function findingHeader(doc: Pdf, fr: FindingWithReview): void {
  const f = fr.finding;
  heading(doc, `${f.type}   (${f.findingId})`, 3);
  table(doc, [{ label: "Machine observation", w: 195 }, { label: "Severity", w: 55 }, { label: "Confidence", w: 55 }, { label: "Analyst review", w: 194 }], [[
    OBSERVATION_LABEL[f.observationStatus], f.severity, pct(f.confidence), REVIEW_LABEL[fr.reviewStatus],
  ]]);
}

function ruleLine(f: Finding): string | undefined {
  const ids = new Set(f.evidence.map((e) => e.measurements?.["ruleId"]).filter((x): x is string => typeof x === "string"));
  const lines = [...ids].map((id) => `Rule ${id}${RULE_NOTES.get(id) ? `: ${RULE_NOTES.get(id)}` : ""}`);
  return lines.length ? lines.join("\n") : undefined;
}

function entityName(b: DossierBundle, entityId: string | null): string {
  const e = b.json.entities.find((x) => x.entityId === entityId);
  return e ? `${e.role} ${e.entityId} (${e.resolution})` : "unresolved";
}

// 3. Contextual analysis ----------------------------------------------------------------------------
function section3Context(doc: Pdf, b: DossierBundle): void {
  heading(doc, "3. Contextual analysis");
  para(doc, "Who the documents appear to be about, and where values that should agree across documents, or within one document, do not. Differences that are normal for the kind of field (for example a permanent and a current address) are not reported.", { muted: true });
  heading(doc, "Entities", 2);
  if (b.json.entities.length === 0) para(doc, "No entities were resolved.");
  else table(doc, [{ label: "Entity", w: 55 }, { label: "Role", w: 80 }, { label: "Resolution", w: 70 }, { label: "Basis", w: W - 205 }],
    b.json.entities.map((e) => [e.entityId, e.role, e.resolution, e.basis.join("; ")]));
  para(doc, "Resolution RESOLVED means a strong identifier matched. SUGGESTED means only a name resembles another entity: it is shown as a lead and is never merged. Findings that depend on an entity that is not RESOLVED are reported with reduced confidence.", { muted: true });

  const items = b.json.findings.filter((f) => ["cross_document", "context", "extraction"].includes(f.finding.category));
  heading(doc, "Findings", 2);
  if (items.length === 0) para(doc, b.generation ? "No cross-document or context observations were produced." : "No reasoning run exists, so there are no contextual findings. That is not a clean result.");
  for (const fr of items) {
    findingHeader(doc, fr);
    para(doc, fr.finding.explanation?.summary ?? fr.finding.evidence[0]?.description ?? "");
    const rule = ruleLine(fr.finding);
    if (rule) para(doc, rule, { muted: true, size: 8 });
    for (const e of fr.finding.evidence) {
      const m = e.measurements;
      if (m && "valueA" in m) para(doc, `Values compared: "${String(m["valueA"])}" and "${String(m["valueB"])}"  (${entityName(b, String(m["entityId"] ?? ""))})`, { indent: 8 });
    }
  }
}

// 4. Visual forensic findings -----------------------------------------------------------------------
const FORENSIC_ANALYZERS = ["image.ela", "image.noise", "image.copymove", "pdf.textlayer", "pdf.fonts", "pdf.revisions", "signature"];

function section4Visual(doc: Pdf, b: DossierBundle): void {
  heading(doc, "4. Visual forensic findings");
  para(doc, "Observations from image and PDF structure analysis. These are signals that a region or a file behaves differently from what is around it. They can have ordinary causes, and none is conclusive on its own.", { muted: true });
  heading(doc, "Which checks ran", 2);
  table(doc, [{ label: "Document", w: 70 }, ...FORENSIC_ANALYZERS.map((a) => ({ label: a, w: (W - 70) / FORENSIC_ANALYZERS.length }))],
    b.documents.map((d) => [d.docId, ...FORENSIC_ANALYZERS.map((a) => (b.json.analyzerStatuses[d.docId]?.[a] ?? "no result").replace("NOT_APPLICABLE", "n/a"))]), 6.5);
  para(doc, "UNAVAILABLE, ERROR and no result mean the check did not run. An absent finding for such a check says nothing about the document.", { muted: true });

  const items = b.json.findings.filter((f) => ["visual", "pdf_structure"].includes(f.finding.category));
  heading(doc, "Findings", 2);
  if (items.length === 0) para(doc, b.generation ? "No visual or PDF structure observations were produced by the checks that ran." : "No reasoning run exists, so there are no forensic findings. That is not a clean result.");
  for (const fr of items) {
    findingHeader(doc, fr);
    for (const e of fr.finding.evidence) para(doc, `${e.description}${e.measurements ? `  [${measurement(e.measurements)}]` : ""}`, { indent: 8, size: 8.5 });
  }
  if (b.regionEvents.length > 0) {
    heading(doc, "Grouped region events", 2);
    table(doc, [{ label: "Event", w: 55 }, { label: "Document / page", w: 110 }, { label: "Detectors", w: 170 }, { label: "Signals", w: 110 }, { label: "Confidence", w: 54 }],
      b.regionEvents.map((e) => [e.eventId, `${e.docId} p${e.page}`, e.detectors.join(", "), e.signalIds.join(", "), pct(e.confidence)]));
  }
  if (b.signals.length > 0) {
    heading(doc, "Raw signals", 2);
    table(doc, [{ label: "Signal", w: 70 }, { label: "Detector", w: 90 }, { label: "Type", w: 120 }, { label: "Confidence", w: 54 }, { label: "Measurements", w: W - 334 }],
      b.signals.map((s) => [s.signalId, s.detector, s.type, pct(s.confidence), measurement(s.measurements)]));
  }
}

// 5. Evidence and traceability -----------------------------------------------------------------------
type ImageFor = (docId: string, page: number) => PdfImage | null;

function section5Evidence(doc: Pdf, b: DossierBundle, imageFor: ImageFor, pageImages: Map<string, Buffer>): void {
  heading(doc, "5. Evidence and traceability");
  para(doc, "For every finding: where in which document it comes from (document, page and bounding box, normalized to the page render) and a crop of that region with the box drawn in red. Boxes mark where a value was read or a signal was found. They do not mark what is wrong.", { muted: true });
  if (b.json.findings.length === 0) return para(doc, "There are no findings to trace.");
  for (const fr of b.json.findings) {
    ensure(doc, 230); // finding header, evidence text and the first row of crops stay together
    findingHeader(doc, fr);
    for (const e of fr.finding.evidence) {
      ensure(doc, 200);
      para(doc, `Evidence ${e.evidenceId} (${e.kind}): ${e.description}`, { bold: true, size: 8.5 });
      if (e.measurements) para(doc, `Measurements: ${measurement(e.measurements)}`, { muted: true, size: 7.5, indent: 8 });
      para(doc, `Produced by ${e.provenance.analyzer} ${e.provenance.analyzerVersion} at ${e.provenance.processedAt}${e.provenance.status !== "AVAILABLE" ? ` (status ${e.provenance.status})` : ""}`, { muted: true, size: 7.5, indent: 8 });
      crops(doc, e, imageFor, pageImages);
    }
  }
}

function crops(doc: Pdf, e: Evidence, imageFor: ImageFor, pageImages: Map<string, Buffer>): void {
  const MAX = 8;
  const frameW = (W - 12) / 2;
  const maxH = 130;
  const shown = e.regions.slice(0, MAX);
  for (let i = 0; i < shown.length; i += 2) {
    const pair = shown.slice(i, i + 2);
    // measure first so the pair is not split across a page
    const geo = pair.map((r) => {
      const img = imageFor(r.docId, r.page);
      const size = pageImages.has(`${r.docId}#${r.page}`) ? pngSize(pageImages.get(`${r.docId}#${r.page}`)!) : undefined;
      return img && size ? { r, img, size, g: cropGeometry(r.bbox, size, frameW, maxH) } : { r, img: null as null, size: undefined, g: undefined };
    });
    const rowH = Math.max(...geo.map((x) => (x.g ? x.g.frame.h : 28))) + 22;
    ensure(doc, rowH + 4);
    const y0 = doc.y;
    geo.forEach((x, k) => drawCrop(doc, x.r, x.img, x.g, M + k * (frameW + 12), y0, frameW));
    doc.x = M;
    doc.y = y0 + rowH;
  }
  if (e.regions.length > MAX) para(doc, `${e.regions.length - MAX} more region(s) of this evidence are listed in the JSON dossier.`, { muted: true, size: 7.5, indent: 8 });
  doc.moveDown(0.2);
}

function drawCrop(doc: Pdf, r: Region, img: PdfImage | null, g: CropGeometry | undefined, x: number, y: number, frameW: number): void {
  const label = `${r.docId} page ${r.page}${r.label ? `, ${r.label}` : ""}`;
  if (!img || !g) {
    doc.rect(x, y, frameW, 24).dash(2, { space: 2 }).strokeColor(C.rule).stroke().undash();
    doc.fillColor(C.warn).font("Helvetica").fontSize(7.5).text(safe(`Page image not available for ${label}`), x + 4, y + 8, { width: frameW - 8, lineBreak: false });
    doc.fillColor(C.text).fontSize(9);
    return;
  }
  const imgW = img.width;
  const imgH = img.height;
  doc.save();
  doc.rect(x, y, g.frame.w, g.frame.h).clip();
  drawImage(doc, img, x - g.window.x * imgW * g.scale, y - g.window.y * imgH * g.scale, imgW * g.scale);
  doc.restore();
  doc.rect(x, y, g.frame.w, g.frame.h).strokeColor(C.rule).lineWidth(0.5).stroke();
  doc.rect(x + g.box.x, y + g.box.y, g.box.w, g.box.h).strokeColor(C.box).lineWidth(1.4).stroke();
  doc.fillColor(C.muted).font("Helvetica").fontSize(7).text(safe(`${label}   ${bbox(r.bbox)}`), x, y + g.frame.h + 3, { width: frameW });
  doc.fillColor(C.text).fontSize(9);
}

// 6. Investigation summary -------------------------------------------------------------------------
function section6Summary(doc: Pdf, b: DossierBundle): void {
  const j = b.json;
  heading(doc, "6. Investigation summary");

  heading(doc, "Anomalies", 2);
  if (j.findings.length === 0) para(doc, b.generation ? "The checks that ran produced no findings. See the limitations below for what did not run." : "No reasoning run exists for this case, so there are no findings. That is not a clean result.");
  else table(doc, [{ label: "Finding", w: 60 }, { label: "Type", w: 130 }, { label: "Category", w: 75 }, { label: "Machine observation", w: 110 }, { label: "Sev.", w: 34 }, { label: "Conf.", w: 30 }, { label: "Analyst review", w: 60 }],
    j.findings.map((fr) => [fr.finding.findingId, fr.finding.type, CATEGORY_LABEL[fr.finding.category], OBSERVATION_LABEL[fr.finding.observationStatus], fr.finding.severity, pct(fr.finding.confidence), REVIEW_LABEL[fr.reviewStatus]]));

  heading(doc, "Potential explanations", 2);
  para(doc, "Ordinary causes that could produce each kind of observation. They are prompts for verification, not statements about this case, unless a finding carries its own explanation.", { muted: true });
  if (j.findings.length === 0) para(doc, "None to list.");
  const own = j.findings.filter((fr) => (fr.finding.explanation?.possibleExplanations.length ?? 0) > 0);
  for (const fr of own) {
    heading(doc, `${fr.finding.type} (${fr.finding.findingId})`, 3);
    bullets(doc, fr.finding.explanation!.possibleExplanations);
  }
  const byCategory = new Map<Finding["category"], string[]>();
  for (const fr of j.findings) {
    if (own.includes(fr)) continue;
    byCategory.set(fr.finding.category, [...(byCategory.get(fr.finding.category) ?? []), fr.finding.findingId]);
  }
  for (const [category, ids] of byCategory) {
    heading(doc, `${CATEGORY_LABEL[category]} observations (${ids.join(", ")})`, 3);
    para(doc, "General considerations for this kind of observation:", { muted: true, size: 8 });
    bullets(doc, GENERAL_CONSIDERATIONS[category]);
  }
  const rules = new Map<string, string[]>();
  for (const fr of j.findings) {
    for (const e of fr.finding.evidence) {
      const id = e.measurements?.["ruleId"];
      if (typeof id === "string" && !(rules.get(id) ?? []).includes(fr.finding.findingId)) rules.set(id, [...(rules.get(id) ?? []), fr.finding.findingId]);
    }
  }
  if (rules.size) {
    heading(doc, "How the rules treat these differences", 3);
    for (const [id, ids] of rules) para(doc, `${id}${RULE_NOTES.get(id) ? `: ${RULE_NOTES.get(id)}` : ""}  (${ids.join(", ")})`, { muted: true, size: 8, indent: 8 });
  }

  heading(doc, "Areas requiring verification", 2);
  if (j.findings.every((f) => f.finding.needsVerification.length === 0)) para(doc, "None recorded.");
  for (const fr of j.findings) {
    if (fr.finding.needsVerification.length === 0) continue;
    para(doc, `${fr.finding.type} (${fr.finding.findingId})`, { bold: true, size: 8.5 });
    bullets(doc, fr.finding.needsVerification);
  }

  heading(doc, "Limitations and analyzer failures", 2);
  if (j.limitations.length === 0) para(doc, "No gaps were recorded: every analyzer that applies ran, and the stored originals match their recorded hashes.");
  else bullets(doc, j.limitations);
  const failing = Object.entries(j.analyzerStatuses).flatMap(([docId, m]) => Object.entries(m).filter(([, s]) => s === "ERROR" || s === "UNAVAILABLE").map(([a, s]) => [docId, a, s]));
  if (failing.length) table(doc, [{ label: "Document", w: 90 }, { label: "Analyzer", w: 160 }, { label: "Status", w: 100 }, { label: "Meaning", w: W - 350 }], failing.map(([d, a, s]) => [d!, a!, s!, "did not run; results missing"]));

  heading(doc, "Integrity of the stored originals", 2);
  para(doc, `Checked ${j.integrity.checkedAt}: ${j.integrity.status}. Each original's SHA-256 is recomputed and compared with the hash recorded at upload.`, { muted: true });
  if (j.integrity.documents.length) table(doc, [{ label: "Document", w: 80 }, { label: "Result", w: 45 }, { label: "Recorded SHA-256", w: 187 }, { label: "Recomputed SHA-256", w: 187 }],
    j.integrity.documents.map((d) => [d.docId, d.status, d.expectedSha256 || "none recorded", d.actualSha256 ?? "file missing or unreadable"]), 6.5);

  heading(doc, "Review outcomes and history", 2);
  if (j.findings.length) table(doc, [{ label: "Finding", w: 150 }, { label: "Current review status", w: 160 }, { label: "Machine observation (unchanged)", w: W - 310 }],
    j.findings.map((fr) => [fr.finding.findingId, REVIEW_LABEL[fr.reviewStatus], OBSERVATION_LABEL[fr.finding.observationStatus]]));
  if (j.reviewHistory.length === 0) para(doc, "No analyst decisions have been recorded.");
  else table(doc, [{ label: "When (UTC)", w: 95 }, { label: "Analyst", w: 55 }, { label: "Action", w: 70 }, { label: "Target", w: 110 }, { label: "Detail", w: W - 330 }],
    j.reviewHistory.map((e) => [e.at, e.actor, e.type, e.targetId, eventDetail(e)]), 7);
}

function eventDetail(e: InvestigationEvent): string {
  const p = e.payload;
  switch (e.type) {
    case "REVIEW": return `${String(p["status"])}${p["reason"] ? `: ${String(p["reason"])}` : ""}`;
    case "COMMENT": return String(p["text"] ?? "");
    case "REOPEN": return `reopened (was ${String(p["previousStatus"] ?? "reviewed")})${p["reason"] ? `: ${String(p["reason"])}` : ""}`;
    case "RECLASSIFY": return `page ${String(p["page"])}: ${String(p["from"])} to ${String(p["to"])}${p["reason"] ? `: ${String(p["reason"])}` : ""}`;
    default: return "";
  }
}

function footers(doc: Pdf, b: DossierBundle): void {
  const range = doc.bufferedPageRange();
  for (let i = 0; i < range.count; i++) {
    doc.switchToPage(i);
    doc.page.margins.bottom = 0; // otherwise writing in the footer area starts a new page
    doc.font("Helvetica").fontSize(7).fillColor(C.muted)
      .text(safe(`TraceDocs dossier ${b.json.caseId}   generated ${b.json.generatedAt}   observations for analyst review, not conclusions`), M, PAGE_H - 36, { width: W - 60, lineBreak: false })
      .text(`Page ${i + 1} of ${range.count}`, M, PAGE_H - 36, { width: W, align: "right", lineBreak: false });
  }
}
