import { describe, expect, it } from "vitest";
import { chmod, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import request from "supertest";
import type { DossierJson, EventsResponse } from "@contracts/api";
import type { ReasoningInput } from "@contracts/reasoning";
import { FIXTURES_DIR, agentWithCase, drain, expectMatchesContract, loggedInAgent, testApp, uploadFixtureDocs } from "./helpers.js";
import { cropGeometry, pngSize, safe, OBSERVATION_LABEL, REVIEW_LABEL } from "../src/reports/dossier.js";

/** Text of an UNCOMPRESSED pdfkit file: text is written as hex strings inside TJ arrays. */
function pdfText(buf: Buffer): string {
  const s = buf.toString("latin1");
  const lines: string[] = [];
  for (const m of s.matchAll(/\[([^\]]*)\]\s*TJ/g)) {
    lines.push([...m[1]!.matchAll(/<([0-9a-fA-F]+)>/g)].map((x) => Buffer.from(x[1]!, "hex").toString("latin1")).join(""));
  }
  return lines.join("\n");
}
/** Wrapped paragraphs are several lines in the PDF; compare them as running text. */
const flat = (s: string) => s.replace(/\s+/g, " ");
const count = (buf: Buffer, re: RegExp) => (buf.toString("latin1").match(re) ?? []).length;
const cropsIn = (buf: Buffer) => count(buf, /\/I\d+ Do/g);
const imagesIn = (buf: Buffer) => count(buf, /\/Subtype \/Image/g);

async function analysed(over: Record<string, string> = {}, depOverrides = {}) {
  const t = await testApp(over, depOverrides);
  const agent = await loggedInAgent(t.app);
  await agent.post("/api/v1/cases").send({ title: "Dossier case" }).expect(201);
  const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
  const plain = () => t.deps.dossier.pdf("CASE-001", "analyst", { compress: false });
  const json = async () => (await agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200)).body as DossierJson;
  return { ...t, agent, mapping, plain, json };
}

const SECTIONS = [
  "1. Document overview", "2. Extracted information", "3. Contextual analysis",
  "4. Visual forensic findings", "5. Evidence and traceability", "6. Investigation summary",
];

describe("GET /cases/:id/dossier?format=json", () => {
  it("matches the DossierJson contract and carries the case's findings, entities, statuses and integrity", async () => {
    const { agent, deps, mapping, json } = await analysed();
    const res = await agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200);
    expectMatchesContract("DossierJson", res.body);
    const d = await json();
    const record = (await deps.evidence.latest("CASE-001"))!;
    expect(d.caseId).toBe("CASE-001");
    expect(new Date(d.generatedAt).toString()).not.toBe("Invalid Date");
    expect(d.findings.map((f) => f.finding.findingId)).toEqual(record.findings.map((f) => f.findingId));
    expect(d.findings.every((f) => f.reviewStatus === "UNREVIEWED")).toBe(true);
    expect(d.assertions).toEqual(record.assertions);
    expect(d.entities).toEqual(record.entities);
    expect(d.summary.case.caseId).toBe("CASE-001");
    expect(d.summary.priority).toEqual(record.priority.perCase);
    expect(Object.keys(d.analyzerStatuses).sort()).toEqual(Object.values(mapping).sort());
    expect(d.analyzerStatuses[mapping["D-02"]!]!["signature"]).toBe("UNAVAILABLE");
    expect(d.integrity.status).toBe("PASS");
    expect(d.integrity.documents).toHaveLength(3);
  });

  it("lists what did not run as limitations: an UNAVAILABLE analyzer is a gap, not a clean result", async () => {
    const { json, mapping } = await analysed();
    const d = await json();
    expect(d.limitations.some((l) => l.includes(mapping["D-02"]!) && l.includes("signature") && l.includes("UNAVAILABLE") && /missing, not clean/.test(l))).toBe(true);
    expect(d.limitations.some((l) => /coverage is 0\.\d+/.test(l) && l.includes("Checks that did not run:") && l.includes("pdf.metadata"))).toBe(true);
  });

  it("includes analyst decisions (review, comment, reopen, reclassify) but not views, uploads or exports; machine observations stay as they were", async () => {
    const { agent, json } = await analysed();
    const before = (await json()).findings.map((f) => f.finding);
    await agent.post("/api/v1/findings/CASE-001-g1-F-0001/review").send({ action: "CONFIRM", reason: "checked" }).expect(200);
    await agent.post("/api/v1/findings/CASE-001-g1-F-0002/review").send({ action: "DISMISS", reason: "same person" }).expect(200);
    await agent.post("/api/v1/findings/CASE-001-g1-F-0002/review").send({ action: "REOPEN" }).expect(200);
    await agent.post("/api/v1/findings/CASE-001-g1-F-0004/review").send({ action: "COMMENT", comment: "looking" }).expect(200);
    await agent.get("/api/v1/findings/CASE-001-g1-F-0001").expect(200); // a VIEW
    const d = await json();
    expect(d.reviewHistory.map((e) => e.type)).toEqual(["REVIEW", "REVIEW", "REOPEN", "COMMENT"]);
    const status = Object.fromEntries(d.findings.map((f) => [f.finding.findingId, f.reviewStatus]));
    expect(status["CASE-001-g1-F-0001"]).toBe("CONFIRMED");
    expect(status["CASE-001-g1-F-0002"]).toBe("UNREVIEWED");
    expect(d.findings.map((f) => f.finding)).toEqual(before);
  });

  it("after a recompute only the current findings are listed, while decisions about the earlier run stay in the history", async () => {
    const { agent, json } = await analysed();
    await agent.post("/api/v1/findings/CASE-001-g1-F-0001/review").send({ action: "CONFIRM" }).expect(200);
    await agent.post("/api/v1/cases/CASE-001/recompute").expect(200);
    const d = await json();
    expect(d.findings.every((f) => f.finding.findingId.startsWith("CASE-001-g2-"))).toBe(true);
    expect(d.findings.every((f) => f.reviewStatus === "UNREVIEWED")).toBe(true);
    expect(d.reviewHistory.map((e) => e.targetId)).toEqual(["CASE-001-g1-F-0001"]);
  });

  it("an empty case gives an honest, empty dossier", async () => {
    const t = await testApp();
    const agent = await agentWithCase(t.app);
    const res = await agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200);
    expectMatchesContract("DossierJson", res.body);
    expect(res.body).toMatchObject({ findings: [], assertions: [], entities: [], reviewHistory: [], analyzerStatuses: {} });
    expect(res.body.limitations).toContain("The case has no documents.");
    expect(res.body.summary.priority).toBeNull();
  });

  it("when reasoning never ran there are no findings and the dossier says that is not a clean result", async () => {
    const { json } = await analysed({}, { reasoningFn: (_i: ReasoningInput) => { throw new Error("engine bug"); } });
    const d = await json();
    expect(d.findings).toEqual([]);
    expect(d.limitations.some((l) => /reasoning engine has not run/.test(l) && /does not mean there are no inconsistencies/.test(l))).toBe(true);
    expect(d.summary.priority).toBeNull();
  });

  it("a document whose analysis failed is listed with the reason, and coverage reflects it", async () => {
    const t = await testApp();
    const agent = await loggedInAgent(t.app);
    await agent.post("/api/v1/cases").send({ title: "x" }).expect(201);
    const up = await agent.post("/api/v1/cases/CASE-001/documents")
      .attach("files", await readFile(path.join(FIXTURES_DIR, "originals", "D-01.png")), "id.png")
      .attach("files", Buffer.from("%PDF-1.4\nnot a fixture\n%%EOF"), "mystery.pdf").expect(202);
    await drain(t.deps);
    const d = (await agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200)).body as DossierJson;
    const failed = up.body.documents[1].docId as string;
    expect(d.limitations.some((l) => l.includes(failed) && /analysis failed/.test(l) && /not the same as a clean result/.test(l))).toBe(true);
    expect(d.analyzerStatuses[failed]).toEqual({});
    expect(d.summary.priority!.coverage).toBeLessThan(1);
  });

  it("a stored original that no longer matches its hash is reported as a limitation", async () => {
    const { json, deps, mapping } = await analysed();
    const file = deps.storage.originalAbsPath(mapping["D-01"]!, "png");
    await chmod(file, 0o644);
    await writeFile(file, Buffer.from("changed"));
    const d = await json();
    expect(d.integrity.status).toBe("FAIL");
    expect(d.limitations.some((l) => l.startsWith(`${mapping["D-01"]}: integrity check FAILED`) && /differs from the one recorded/.test(l))).toBe(true);
  });
});

describe("dossier route behaviour", () => {
  it("serves a PDF by default with the right headers, and logs an EXPORT event per export", async () => {
    const { agent } = await analysed();
    const res = await agent.get("/api/v1/cases/CASE-001/dossier").buffer(true).parse((r, cb) => {
      const c: Buffer[] = []; r.on("data", (d: Buffer) => c.push(d)); r.on("end", () => cb(null, Buffer.concat(c)));
    }).expect(200);
    expect(res.headers["content-type"]).toBe("application/pdf");
    expect(res.headers["content-disposition"]).toBe('attachment; filename="CASE-001-dossier.pdf"');
    expect(res.headers["cache-control"]).toBe("no-store");
    const pdf = res.body as Buffer;
    expect(pdf.subarray(0, 5).toString()).toBe("%PDF-");
    expect(pdf.subarray(-8).toString()).toContain("%%EOF");
    await agent.get("/api/v1/cases/CASE-001/dossier?format=pdf").expect(200);
    await agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200);
    const events = ((await agent.get("/api/v1/cases/CASE-001/events")).body as EventsResponse).events.filter((e) => e.type === "EXPORT");
    expect(events.map((e) => e.payload["format"])).toEqual(["pdf", "pdf", "json"]);
    expect(events[0]).toMatchObject({ actor: "analyst", targetId: "CASE-001", payload: { generation: 1, findings: 6, integrity: "PASS" } });
    expect(events[0]!.payload["bytes"]).toBe(pdf.length);
  });

  it("400 for an unknown format, 404 for an unknown case, 401 without login, and nothing is logged for a rejected request", async () => {
    const { agent, app } = await analysed();
    await agent.get("/api/v1/cases/CASE-001/dossier?format=docx").expect(400);
    await agent.get("/api/v1/cases/CASE-001/dossier?x=1").expect(400);
    expect((await agent.get("/api/v1/cases/CASE-404/dossier").expect(404)).body.error.code).toBe("CASE_NOT_FOUND");
    expect((await request(app).get("/api/v1/cases/CASE-001/dossier")).status).toBe(401);
    const events = ((await agent.get("/api/v1/cases/CASE-001/events")).body as EventsResponse).events;
    expect(events.filter((e) => e.type === "EXPORT")).toEqual([]);
  });

  it("the default (compressed) PDF is valid and smaller than the uncompressed one", async () => {
    const { deps, plain } = await analysed();
    const packed = await deps.dossier.pdf("CASE-001", "analyst");
    const raw = await plain();
    expect(packed.length).toBeLessThan(raw.length);
    expect(packed.subarray(0, 5).toString()).toBe("%PDF-");
  });
});

describe("PDF content", () => {
  it("has every section heading, in order, and the summary sub-headings", async () => {
    const { plain } = await analysed();
    const text = pdfText(await plain());
    const at = SECTIONS.map((h) => text.indexOf(h));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    for (const h of ["Entities", "Anomalies", "Potential explanations", "Areas requiring verification", "Limitations and analyzer failures", "Integrity of the stored originals", "Review outcomes and history", "Which checks ran"]) {
      expect(text).toContain(h);
    }
    expect(text).toContain("TraceDocs Investigation Dossier");
  });

  it("shows region crops: one per evidence region, from the real page renders", async () => {
    const { plain, deps } = await analysed();
    const pdf = await plain();
    const record = (await deps.evidence.latest("CASE-001"))!;
    const expected = record.findings.flatMap((f) => f.evidence).reduce((n, e) => n + Math.min(8, e.regions.length), 0);
    expect(expected).toBeGreaterThan(8);
    expect(cropsIn(pdf)).toBe(expected);
    const pages = new Set(record.findings.flatMap((f) => f.evidence.flatMap((e) => e.regions.map((r) => `${r.docId}#${r.page}`))));
    expect(imagesIn(pdf)).toBe(pages.size); // each page image is embedded once and reused for all its crops
  });

  it("states document, page and bounding box for every region, and each document's file name, hash and type", async () => {
    const { plain, deps, mapping } = await analysed();
    const text = pdfText(await plain());
    const record = (await deps.evidence.latest("CASE-001"))!;
    const r = record.findings[0]!.evidence[0]!.regions[0]!;
    expect(text).toContain(`${r.docId} page ${r.page}`);
    expect(text).toContain(`x ${r.bbox[0].toFixed(3)}, y ${r.bbox[1].toFixed(3)}`);
    for (const [fx, real] of Object.entries(mapping)) {
      const doc = (await deps.documents.get(real))!;
      expect(text).toContain(doc.filename);
      expect(text).toContain(doc.sha256.slice(0, 16));
      void fx;
    }
    expect(text).toMatch(/Document type\s*\n?.*(id_card|application_form|bank_statement)/s);
  });

  it("prints the extracted values with OCR and extraction confidence", async () => {
    const { plain, deps, mapping } = await analysed();
    const text = pdfText(await plain());
    const analysis = (await deps.analysis.get(mapping["D-02"]!))!;
    for (const f of analysis.fields.slice(0, 4)) expect(text).toContain(f.rawValue.slice(0, 12));
    expect(flat(text)).toContain("confidence of the character recognition");
    expect(flat(text)).toContain("confidence that the value belongs to this field");
    for (const f of analysis.fields.slice(0, 3)) {
      expect(text).toContain(f.ocrConfidence.toFixed(2));
      expect(text).toContain(f.extractionConfidence.toFixed(2));
    }
  });

  it("distinguishes the machine's observation from the analyst's review, side by side", async () => {
    const { agent, plain } = await analysed();
    await agent.post("/api/v1/findings/CASE-001-g1-F-0001/review").send({ action: "CONFIRM" }).expect(200);
    await agent.post("/api/v1/findings/CASE-001-g1-F-0002/review").send({ action: "DISMISS", reason: "same person" }).expect(200);
    const text = pdfText(await plain());
    expect(text).toContain(OBSERVATION_LABEL.INCONSISTENT);
    expect(text).toContain(OBSERVATION_LABEL.POTENTIAL_ANOMALY);
    expect(text).toContain(REVIEW_LABEL.CONFIRMED);
    expect(text).toContain(REVIEW_LABEL.DISMISSED);
    expect(text).toContain(REVIEW_LABEL.UNREVIEWED);
    expect(text).toContain("same person"); // the dismissal reason is in the history
  });

  it("never uses accusatory or intent language", async () => {
    const { agent, plain } = await analysed();
    await agent.post("/api/v1/findings/CASE-001-g1-F-0001/review").send({ action: "CONFIRM" }).expect(200);
    const text = pdfText(await plain());
    expect(text).not.toMatch(/forg(ed|ery|e)\b|fraud|intentional|deliberate|tampered by|tampering by|culprit|guilty/i);
    expect(text).toContain("not conclusions");
    expect(text).toContain("Priority orders cases for human review");
  });

  it("lists limitations, analyzer failures and integrity in the summary", async () => {
    const { plain, mapping } = await analysed();
    const text = pdfText(await plain());
    expect(text).toContain("signature was UNAVAILABLE");
    expect(text).toContain(mapping["D-02"]!);
    expect(text).toMatch(/Checked .*: PASS/);
  });

  it("an empty case still renders, and says there is nothing to conclude", async () => {
    const t = await testApp();
    const agent = await agentWithCase(t.app);
    const text = pdfText(await t.deps.dossier.pdf("CASE-001", "analyst", { compress: false }));
    for (const h of SECTIONS) expect(text).toContain(h);
    expect(text).toContain("This case has no documents.");
    expect(text).toContain("The case has no documents.");
    void agent;
  });

  it("without a reasoning run it says the absence of findings is not a clean result", async () => {
    const { plain } = await analysed({}, { reasoningFn: () => { throw new Error("engine bug"); } });
    const text = pdfText(await plain());
    expect(text).toContain("No reasoning run exists");
    expect(text).toContain("That is not a clean result");
  });

  it("still renders when a page image is missing, and says so for each affected region", async () => {
    const { plain, deps, mapping } = await analysed();
    const full = cropsIn(await plain());
    await rm(deps.storage.derivedFile(mapping["D-02"]!, "page-1.png"));
    const pdf = await plain();
    const text = pdfText(pdf);
    expect(text).toContain(`Page image not available for ${mapping["D-02"]} page 1`);
    expect(cropsIn(pdf)).toBeLessThan(full);
    expect(cropsIn(pdf)).toBeGreaterThan(0);
    for (const h of SECTIONS) expect(text).toContain(h);
  });

  it("notes an analyst's page reclassification next to the extracted fields", async () => {
    const { agent, deps, plain, mapping } = await analysed();
    await agent.patch(`/api/v1/documents/${mapping["D-03"]}/pages/1/type`).send({ docType: "other", reason: "account summary" }).expect(202);
    await drain(deps);
    const text = pdfText(await plain());
    expect(text).toContain("was reclassified by analyst from bank_statement to other");
    expect(text).toContain("account summary");
  });

  it("page numbers in the footers match the real page count", async () => {
    const { plain } = await analysed();
    const pdf = await plain();
    const pages = count(pdf, /\/Type \/Page\b/g);
    expect(pages).toBeGreaterThanOrEqual(7);
    expect(pdfText(pdf)).toContain(`Page ${pages} of ${pages}`);
  });

  it("copes with characters the built-in fonts cannot show (shown as '?', never a crash)", async () => {
    const t = await testApp();
    const agent = await loggedInAgent(t.app);
    await agent.post("/api/v1/cases").send({ title: "Rupee ₹ and 日本語 case" }).expect(201);
    await agent.post("/api/v1/cases/CASE-001/documents").attach("files", await readFile(path.join(FIXTURES_DIR, "originals", "D-01.png")), "scan ₹ 写真.png").expect(202);
    await drain(t.deps);
    const text = pdfText(await t.deps.dossier.pdf("CASE-001", "analyst", { compress: false }));
    expect(text).toContain("Rupee ? and ??? case");
  });
});

describe("helpers", () => {
  it("safe() keeps Latin-1 and common typographic marks, replaces the rest, strips control characters", () => {
    expect(safe("Café – “quoted” … €5")).toBe("Café – “quoted” … €5");
    expect(safe("₹ 日本 😀")).toBe("? ?? ?");
    expect(safe("a\u0000b\u0007c")).toBe("a b c");
    expect(safe("line1\nline2")).toBe("line1\nline2");
    expect(safe(undefined)).toBe("");
    expect(safe(12.5)).toBe("12.5");
  });

  it("pngSize reads the real size of a render, and rejects anything else", async () => {
    const png = await readFile(path.join(FIXTURES_DIR, "pages", "D-01-page-1.png"));
    const s = pngSize(png)!;
    expect(s.w).toBeGreaterThan(500);
    expect(s.h).toBeGreaterThan(s.w);
    expect(pngSize(Buffer.from("not a png at all, long enough to pass the length check"))).toBeUndefined();
    expect(pngSize(Buffer.alloc(4))).toBeUndefined();
  });

  describe("cropGeometry", () => {
    const img = { w: 1240, h: 1754 };
    const inside = (g: ReturnType<typeof cropGeometry>) => {
      expect(g.window.x).toBeGreaterThanOrEqual(0);
      expect(g.window.y).toBeGreaterThanOrEqual(0);
      expect(g.window.x + g.window.w).toBeLessThanOrEqual(1 + 1e-9);
      expect(g.window.y + g.window.h).toBeLessThanOrEqual(1 + 1e-9);
      expect(g.box.x).toBeGreaterThanOrEqual(-1e-6);
      expect(g.box.y).toBeGreaterThanOrEqual(-1e-6);
      expect(g.box.x + g.box.w).toBeLessThanOrEqual(g.frame.w + 1e-6);
      expect(g.box.y + g.box.h).toBeLessThanOrEqual(g.frame.h + 1e-6);
    };
    it("always contains the region and fits the frame", () => {
      for (const r of [[0.3, 0.4, 0.2, 0.03], [0, 0, 0.1, 0.02], [0.9, 0.97, 0.1, 0.03], [0.1, 0.1, 0.8, 0.5], [0.45, 0.5, 0.01, 0.005]] as [number, number, number, number][]) {
        const g = cropGeometry(r, img, 235, 130);
        inside(g);
        expect(g.frame.w).toBeLessThanOrEqual(235 + 1e-6);
        expect(g.frame.h).toBeLessThanOrEqual(130 + 1e-6);
      }
    });
    it("gives a tiny region context instead of an extreme zoom", () => {
      const g = cropGeometry([0.45, 0.5, 0.01, 0.005], img, 235, 130);
      expect(g.window.w).toBeGreaterThanOrEqual(0.4);
      expect(g.box.w).toBeLessThan(g.frame.w / 10);
    });
    it("places the box at the right spot (scale-consistent)", () => {
      const r: [number, number, number, number] = [0.3, 0.4, 0.2, 0.03];
      const g = cropGeometry(r, img, 235, 130);
      expect(g.box.x).toBeCloseTo((0.3 - g.window.x) * img.w * g.scale, 6);
      expect(g.box.w).toBeCloseTo(0.2 * img.w * g.scale, 6);
    });
  });
});
