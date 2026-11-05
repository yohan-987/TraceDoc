import { Router, type RequestHandler } from "express";
import { z } from "zod";
import { validate } from "../validation.js";
import { SAFE_ID_RE } from "../services/storage.js";
import type { CaseService } from "../services/cases.js";
import type { DossierService } from "../reports/service.js";

const params = z.object({ caseId: z.string().regex(SAFE_ID_RE, "invalid case id") });
const query = z.object({ format: z.enum(["pdf", "json"]).default("pdf") }).strict();

/** GET /cases/:caseId/dossier: the investigation dossier as a PDF (default) or, with ?format=json, as raw structure. */
export function dossierRouter(d: { cases: CaseService; dossier: DossierService; requireAuth: RequestHandler }): Router {
  const r = Router();
  r.get("/cases/:caseId/dossier", d.requireAuth, validate("params", params), validate("query", query), async (req, res, next) => {
    try {
      const caseId = (req.params as { caseId: string }).caseId;
      const { format } = req.query as z.infer<typeof query>;
      await d.cases.require(caseId);
      const actor = req.analyst!.analystId;
      if (format === "json") {
        res.json(await d.dossier.json(caseId, actor));
        return;
      }
      const pdf = await d.dossier.pdf(caseId, actor);
      res
        .status(200)
        .set({ "Content-Type": "application/pdf", "Content-Disposition": `attachment; filename="${caseId}-dossier.pdf"`, "Cache-Control": "no-store" })
        .send(pdf);
    } catch (err) {
      next(err);
    }
  });
  return r;
}
