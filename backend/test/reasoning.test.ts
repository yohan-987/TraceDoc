import { describe, expect, it } from "vitest";
import request from "supertest";
import type { CaseSummary } from "@contracts/api";
import type { ReasoningInput, ReasoningOutput } from "@contracts/reasoning";
import type { AnalyzeResponse, Finding, RegionEvent } from "@contracts/types";
import { drain, expectMatchesContract, fixtureJson, loggedInAgent, testApp, uploadFixtureDocs } from "./helpers.js";
import { remapIds } from "../src/services/remap.js";
import { buildGraph } from "../src/services/graph.js";

type Agent = Awaited<ReturnType<typeof loggedInAgent>>;

interface ExpectedRow {
  expectedId: string; type: string; category?: string; observationStatus?: string; severity?: string;
  assertionIds?: string[]; signalIds?: string[]; maxConfidence?: number;
}
interface NonFindingRow { nonFindingId: string; assertionIds: string[]; forbiddenTypes: string[] }

const evidenceAssertionIds = (f: Finding) => new Set(f.evidence.flatMap((e) => e.assertionIds ?? []));

/** The rule from contracts/fixtures/README.md, applied to real engine output. */
async function checkExpectations(caseId: string, mapping: Record<string, string>, findings: Finding[], events: RegionEvent[]) {
  const expected = remapIds((await fixtureJson<Record<string, ExpectedRow[]>>("expected_findings.json"))[caseId] ?? [], mapping);
  const forbidden = remapIds((await fixtureJson<Record<string, NonFindingRow[]>>("expected_non_findings.json"))[caseId] ?? [], mapping);
  const problems: string[] = [];
  for (const row of expected) {
    const hit = findings.find((f) => {
      if (f.type !== row.type) return false;
      if (row.category && f.category !== row.category) return false;
      if (row.observationStatus && f.observationStatus !== row.observationStatus) return false;
      if (row.severity && f.severity !== row.severity) return false;
      if (row.maxConfidence !== undefined && f.confidence > row.maxConfidence) return false;
      if (row.assertionIds) {
        const have = evidenceAssertionIds(f);
        return row.assertionIds.every((id) => have.has(id));
      }
      if (row.signalIds) return events.some((e) => row.signalIds!.every((s) => e.signalIds.includes(s)));
      return true;
    });
    if (!hit) problems.push(`missing expected finding ${row.expectedId} (${row.type})`);
  }
  for (const nf of forbidden) {
    for (const f of findings) {
      const have = evidenceAssertionIds(f);
      const typeForbidden = nf.forbiddenTypes.includes("*") || nf.forbiddenTypes.includes(f.type);
      if (typeForbidden && nf.assertionIds.every((id) => have.has(id))) problems.push(`${nf.nonFindingId} violated by ${f.type}`);
    }
  }
  return { problems, expectedCount: expected.length, forbiddenCount: forbidden.length };
}

async function twoCases(t: Awaited<ReturnType<typeof testApp>>) {
  const agent = await loggedInAgent(t.app);
  await agent.post("/api/v1/cases").send({ title: "one" }).expect(201);
  await agent.post("/api/v1/cases").send({ title: "two" }).expect(201);
  return agent;
}

describe("reasoning runs by itself when the last document finishes", () => {
  it("CASE-001 with the real engine: expected findings present, expected non-findings absent, priority has confidence and coverage", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);

    const record = (await t.deps.evidence.latest("CASE-001"))!;
    expect(record.generation).toBe(1);
    const { problems, expectedCount, forbiddenCount } = await checkExpectations("CASE-001", mapping, record.findings, record.events);
    expect(expectedCount).toBe(6);
    expect(forbiddenCount).toBe(5);
    expect(problems).toEqual([]);

    // the API serves it
    const list = (await agent.get("/api/v1/cases/CASE-001/findings").expect(200)).body;
    expectMatchesContract("FindingsResponse", list);
    expect(list.findings.length).toBe(record.findings.length);

    // priority: band, confidence and coverage are separate numbers
    const summary = (await agent.get("/api/v1/cases/CASE-001").expect(200)).body as CaseSummary;
    expect(summary.priority).toMatchObject({ priorityBand: expect.any(String), score: expect.any(Number), confidence: expect.any(Number), coverage: expect.any(Number) });
    // D-02's signature analyzer is UNAVAILABLE in the fixture, and the fixtures never ran the PDF metadata/revision checks or the
    // field extractor: expected-but-absent analyzers count as UNAVAILABLE (B15). Coverage says so, the band does not move.
    expect(summary.priority!.coverage).toBe(0.667);
    expect(summary.priority!.incompleteCoverage).toBe(true);
    expect(summary.priority!.missingAnalyzers).toEqual(expect.arrayContaining(["signature", "pdf.metadata", "pdf.revisions", "page.field_extractor"]));
    expect(summary.priority!.confidence).not.toBe(summary.priority!.coverage);
    expect(summary.perDocumentPriority[mapping["D-02"]!]!.coverage).toBe(0.667);
    expect(Object.keys(summary.perDocumentPriority).sort()).toEqual(Object.values(mapping).sort());
  });

  it("the engine output equals the recorded fixture output (re-labelled), apart from the store's finding ids", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    const record = (await t.deps.evidence.latest("CASE-001"))!;
    const fixture = remapIds(await fixtureJson<ReasoningOutput>("CASE-001.reasoning.json"), mapping);
    const unspace = (id: string) => id.replace(/^CASE-001-g1-/, "");
    expect(record.assertions).toEqual(fixture.assertions);
    expect(record.entities).toEqual(fixture.entities);
    expect(record.events).toEqual(fixture.events);
    expect(record.findings.map((f) => ({ ...f, findingId: unspace(f.findingId) }))).toEqual(fixture.findings);
    expect(record.priority.perCase.contributingFindingIds.map(unspace)).toEqual(fixture.priority.perCase.contributingFindingIds);
    // the recorded fixture predates expected-analyzer coverage (B15): compare everything except the coverage fields
    const noCoverage = (p: object) => { const { coverage: _c, missingAnalyzers: _m, incompleteCoverage: _i, ...rest } = p as Record<string, unknown>; return rest; };
    expect(noCoverage({ ...record.priority.perCase, contributingFindingIds: [] })).toEqual(noCoverage({ ...fixture.priority.perCase, contributingFindingIds: [] }));
  });

  it("CASE-002 (similar names, different people): no findings, two entities kept apart, coverage reported honestly", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-002", ["D-11.png", "D-12.png", "D-13.png"]);
    const record = (await t.deps.evidence.latest("CASE-002"))!;
    expect(record.findings).toEqual([]);
    expect((await checkExpectations("CASE-002", mapping, record.findings, record.events)).problems).toEqual([]);
    expect(record.entities.map((e) => e.role)).toEqual(["applicant", "guarantor"]);
    expect(record.priority.perCase).toMatchObject({ priorityBand: "low", score: 0, coverage: 0.833 });
    // images only: the pdf.* family is NOT_APPLICABLE, but the field extractor never ran on these fixtures
    expect(record.priority.perCase.missingAnalyzers).toEqual(["page.field_extractor"]);
    const g = (await agent.get("/api/v1/cases/CASE-002/graph").expect(200)).body;
    expectMatchesContract("GraphResponse", g);
    expect(g).toEqual(remapIds(await fixtureJson("CASE-002.graph.json"), mapping));
  });

  it("waits until every document has finished", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    const up = await agent
      .post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(new URL("../../contracts/fixtures/originals/D-01.png", import.meta.url)), "a.png")
      .attach("files", (await import("node:fs")).readFileSync(new URL("../../contracts/fixtures/originals/D-02.png", import.meta.url)), "b.png")
      .attach("files", (await import("node:fs")).readFileSync(new URL("../../contracts/fixtures/originals/D-03.pdf", import.meta.url)), "c.pdf")
      .expect(202);
    expect(up.body.jobs).toHaveLength(3);
    await t.deps.worker.processNext();
    expect(await t.deps.evidence.latest("CASE-001")).toBeUndefined();
    await t.deps.worker.processNext();
    expect(await t.deps.evidence.latest("CASE-001")).toBeUndefined();
    await t.deps.worker.processNext(); // the last one
    expect((await t.deps.evidence.latest("CASE-001"))!.generation).toBe(1);
    expect(await t.deps.worker.processNext()).toBeUndefined();
    expect((await t.deps.evidence.generations("CASE-001")).length).toBe(1);
  });

  it("passes the stored analyses to the engine in upload order", async () => {
    const seen: ReasoningInput[] = [];
    const t = await testApp({}, { reasoningFn: (input) => { seen.push(input); return emptyOutput(input); } });
    const agent = await twoCases(t);
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-03.pdf", "D-01.png", "D-02.png"]);
    expect(seen).toHaveLength(1);
    expect(seen[0]!.caseId).toBe("CASE-001");
    expect(seen[0]!.documents.map((d) => d.docId)).toEqual([mapping["D-03"], mapping["D-01"], mapping["D-02"]]);
    for (const d of seen[0]!.documents) expect(d).toEqual(await t.deps.analysis.get(d.docId));
  });
});

function emptyOutput(input: ReasoningInput): ReasoningOutput {
  const p = { priorityBand: "low" as const, score: 0, confidence: 0, coverage: 1, contributingFindingIds: [] };
  return {
    assertions: [], entities: [], findings: [], events: [],
    priority: {
      perCase: p, perDocument: Object.fromEntries(input.documents.map((d) => [d.docId, p])),
      provenance: { analyzer: "test", analyzerVersion: "0", config: {}, inputSha256: "0".repeat(64), processedAt: "2026-11-01T09:00:00Z", status: "AVAILABLE" },
    },
  };
}

describe("a failed or partial analysis is never read as clean", () => {
  it("a document whose analysis failed still counts: coverage drops and the other documents are still reasoned over", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    const fs = await import("node:fs");
    const up = await agent
      .post("/api/v1/cases/CASE-001/documents")
      .attach("files", fs.readFileSync(new URL("../../contracts/fixtures/originals/D-01.png", import.meta.url)), "id.png")
      .attach("files", fs.readFileSync(new URL("../../contracts/fixtures/originals/D-02.png", import.meta.url)), "form.png")
      .attach("files", Buffer.from("%PDF-1.4\nnot a known fixture\n%%EOF"), "mystery.pdf")
      .expect(202);
    await drain(t.deps);
    const [idDoc, formDoc, failedDoc] = up.body.documents.map((d: { docId: string }) => d.docId) as string[];

    const record = (await t.deps.evidence.latest("CASE-001"))!;
    // DOB conflict between the two good documents is still found
    expect(record.findings.some((f) => f.type === "DOB_CONFLICT")).toBe(true);
    // but the case is not reported as fully covered
    expect(record.priority.perCase.coverage).toBeLessThan(1);
    expect(record.priority.perDocument[failedDoc!]).toMatchObject({ coverage: 0 });
    expect(record.priority.perDocument[idDoc!]!.coverage).toBe(0.833); // not 1: the field extractor is expected but absent from the fixture
    void formDoc;
  });

  it("PARTIAL documents (an analyzer UNAVAILABLE) lower coverage but not the band", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    // D-02 has signature UNAVAILABLE in the fixture, so CASE-001 is PARTIAL on one document
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    const record = (await t.deps.evidence.latest("CASE-001"))!;
    const fixture = await fixtureJson<ReasoningOutput>("CASE-001.reasoning.json");
    expect(record.priority.perCase.priorityBand).toBe(fixture.priority.perCase.priorityBand);
    expect(record.priority.perCase.score).toBe(fixture.priority.perCase.score);
    expect(record.priority.perCase.coverage).toBeLessThan(1);
    expect(record.priority.perDocument[mapping["D-02"]!]!.coverage).toBe(0.667);
    expect(record.priority.perDocument[mapping["D-01"]!]!.coverage).toBe(0.833);
  });

  it("if the engine throws nothing is stored, the worker keeps going and the case has no priority", async () => {
    const t = await testApp({}, { reasoningFn: () => { throw new Error("engine bug"); } });
    const agent = await twoCases(t);
    await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    expect(await t.deps.evidence.latest("CASE-001")).toBeUndefined();
    const jobs = (await agent.get("/api/v1/cases/CASE-001").expect(200)).body as CaseSummary;
    expect(jobs.jobs.every((j) => j.status === "DONE" || j.status === "PARTIAL")).toBe(true);
    expect(jobs.priority).toBeNull();
    const res = await agent.post("/api/v1/cases/CASE-001/recompute").expect(500);
    expect(res.body.error.code).toBe("INTERNAL");
  });
});

describe("POST /cases/:id/recompute", () => {
  it("writes a new generation from the same analyses and leaves the old one untouched", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    const first = (await t.deps.evidence.latest("CASE-001"))!;

    const res = await agent.post("/api/v1/cases/CASE-001/recompute").expect(200);
    expectMatchesContract("CaseSummary", res.body);
    const summary = res.body as CaseSummary;
    expect(summary.priority!.contributingFindingIds[0]).toMatch(/^CASE-001-g2-/);
    expect(summary.documents).toHaveLength(3);

    const gens = await t.deps.evidence.generations("CASE-001");
    expect(gens.map((g) => g.generation)).toEqual([1, 2]);
    const second = (await t.deps.evidence.latest("CASE-001"))!;
    // deterministic engine: same findings, only the store's ids differ
    expect(second.findings.map((f) => f.type)).toEqual(first.findings.map((f) => f.type));
    expect(second.priority.perCase.score).toBe(first.priority.perCase.score);
    expect((await t.deps.evidence.get("CASE-001", 1))!.findings[0]!.findingId).toBe("CASE-001-g1-F-0001");
    // the old finding is still readable
    await agent.get("/api/v1/findings/CASE-001-g1-F-0001").expect(200);
    await agent.get("/api/v1/findings/CASE-001-g2-F-0001").expect(200);
  });

  it("does not re-run analysis", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    const before = (await t.deps.jobs.list()).length;
    await agent.post("/api/v1/cases/CASE-001/recompute").expect(200);
    expect((await t.deps.jobs.list()).length).toBe(before);
    expect(await t.deps.worker.processNext()).toBeUndefined();
  });

  it("409 while analysis is still running, 409 with no documents, 404 for an unknown case, 401 without login", async () => {
    const t = await testApp();
    const agent = await twoCases(t);
    expect((await agent.post("/api/v1/cases/CASE-001/recompute").expect(409)).body.error.code).toBe("NO_DOCUMENTS");
    await agent
      .post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(new URL("../../contracts/fixtures/originals/D-01.png", import.meta.url)), "a.png")
      .expect(202); // job queued, worker not run yet
    expect((await agent.post("/api/v1/cases/CASE-001/recompute").expect(409)).body.error.code).toBe("ANALYSIS_PENDING");
    expect((await agent.post("/api/v1/cases/CASE-404/recompute").expect(404)).body.error.code).toBe("CASE_NOT_FOUND");
    expect((await request(t.app).post("/api/v1/cases/CASE-001/recompute")).status).toBe(401);
  });
});

describe("REASONING_MODE=stub", () => {
  it("returns the recorded fixture output, re-labelled with real document ids", async () => {
    const t = await testApp({ REASONING_MODE: "stub" });
    const agent = await twoCases(t);
    const mapping = await uploadFixtureDocs(t.deps, agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    const record = (await t.deps.evidence.latest("CASE-001"))!;
    const fixture = remapIds(await fixtureJson<ReasoningOutput>("CASE-001.reasoning.json"), mapping);
    expect(record.findings.map((f) => f.type)).toEqual(fixture.findings.map((f) => f.type));
    expect(record.assertions).toEqual(fixture.assertions);
    expect(JSON.stringify(record)).not.toMatch(/"D-0[123]"/);
  });

  it("says so for a case it has no recording for", async () => {
    const t = await testApp({ REASONING_MODE: "stub" });
    const agent = await loggedInAgent(t.app);
    for (let i = 0; i < 3; i++) await agent.post("/api/v1/cases").send({ title: `c${i}` }).expect(201);
    await uploadFixtureDocs(t.deps, agent, "CASE-003", ["D-01.png"]); // job finishes, stub has no CASE-003 recording
    expect(await t.deps.evidence.latest("CASE-003")).toBeUndefined();
    expect((await agent.post("/api/v1/cases/CASE-003/recompute").expect(409)).body.error.code).toBe("NO_STUB_REASONING");
  });
});

describe("graph suggested edges use suggestedLinks", () => {
  const base = { caseId: "C", documents: [], docTypes: {}, fieldNames: {}, assertions: [], findings: [] };
  const entity = (over: object) => ({ entityId: "E-002", caseId: "C", role: "account_holder" as const, resolution: "SUGGESTED" as const, basis: ["no shared identifier"], assertionIds: [], ...over });
  const e1 = { entityId: "E-001", caseId: "C", role: "applicant" as const, resolution: "RESOLVED" as const, basis: [], assertionIds: [] };

  it("follows the structured link even when the basis text names no entity", () => {
    const g = buildGraph({ ...base, entities: [e1, entity({ suggestedLinks: [{ entityId: "E-001", score: 97, method: "name_token_sort" }] })] });
    expect(g.edges).toEqual([{ id: "G-001", source: "E-002", target: "E-001", kind: "suggested", label: "suggested link" }]);
  });

  it("prefers the best-scoring link", () => {
    const e3 = { ...e1, entityId: "E-003" };
    const g = buildGraph({
      ...base,
      entities: [e1, e3, entity({ suggestedLinks: [{ entityId: "E-001", score: 93, method: "name_token_sort" }, { entityId: "E-003", score: 99, method: "name_token_sort" }] })],
    });
    expect(g.edges[0]).toMatchObject({ source: "E-002", target: "E-003" });
  });

  it("falls back to the entity id named in the basis for older records", () => {
    const g = buildGraph({ ...base, entities: [e1, entity({ basis: ["name similarity 100 to E-001 (suggested only)"] })] });
    expect(g.edges).toEqual([{ id: "G-001", source: "E-002", target: "E-001", kind: "suggested", label: "name similarity only" }]);
  });

  it("draws no edge when nothing names a target", () => {
    expect(buildGraph({ ...base, entities: [e1, entity({})] }).edges).toEqual([]);
  });
});

void ({} as AnalyzeResponse);
