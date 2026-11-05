import { readFile } from "node:fs/promises";
import type { DossierJson } from "@contracts/api";
import type { JsonlEventLog } from "../services/eventLog.js";
import type { Storage } from "../services/storage.js";
import type { DossierBuilder, DossierBundle } from "./bundle.js";
import { renderDossierPdf } from "./dossier.js";

/** Builds the dossier (JSON or PDF) and records the export in the investigation log. */
export class DossierService {
  constructor(
    private readonly builder: DossierBuilder,
    private readonly storage: Storage,
    private readonly log: JsonlEventLog,
  ) {}

  async json(caseId: string, actor: string): Promise<DossierJson> {
    const bundle = await this.builder.build(caseId);
    await this.logExport(bundle, actor, "json");
    return bundle.json;
  }

  async pdf(caseId: string, actor: string, opts: { compress?: boolean } = {}): Promise<Buffer> {
    const bundle = await this.builder.build(caseId);
    const pdf = await renderDossierPdf(bundle, { pageImages: await this.loadPageImages(bundle), compress: opts.compress ?? true });
    await this.logExport(bundle, actor, "pdf", pdf.length);
    return pdf;
  }

  /** The canonical renders of every page that has evidence. A page that cannot be read is left out and reported in the PDF. */
  private async loadPageImages(bundle: DossierBundle): Promise<Map<string, Buffer>> {
    const keys = new Map<string, { docId: string; page: number }>();
    for (const { finding } of bundle.json.findings) {
      for (const e of finding.evidence) for (const r of e.regions) keys.set(`${r.docId}#${r.page}`, { docId: r.docId, page: r.page });
    }
    const images = new Map<string, Buffer>();
    for (const [key, { docId, page }] of keys) {
      try {
        images.set(key, await readFile(this.storage.derivedFile(docId, `page-${page}.png`)));
      } catch {
        /* no render on disk: the PDF says so for that region */
      }
    }
    return images;
  }

  private async logExport(b: DossierBundle, actor: string, format: "pdf" | "json", bytes?: number): Promise<void> {
    await this.log.append({
      caseId: b.json.caseId,
      actor,
      type: "EXPORT",
      targetId: b.json.caseId,
      payload: { format, generation: b.generation ?? null, findings: b.json.findings.length, integrity: b.json.integrity.status, ...(bytes ? { bytes } : {}) },
    });
  }
}
