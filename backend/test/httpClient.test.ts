import { afterEach, describe, expect, it } from "vitest";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { readdir } from "node:fs/promises";
import path from "node:path";
import type { AnalyzeResponse, Job } from "@contracts/types";
import type { CaseSummary, DocumentDetail, DossierJson } from "@contracts/api";
import { FIXTURES_DIR, fixtureJson, loggedInAgent, testApp, drain, uploadFixtureDocs } from "./helpers.js";
import { HttpAnalysisClient, createAnalyzeValidator } from "../src/services/httpAnalysisClient.js";
import { AnalysisTimeoutError, PermanentAnalysisError } from "../src/services/analysisClient.js";
import { MockAnalysisClient } from "../src/services/mockAnalysisClient.js";
import { createLogger } from "../src/logger.js";
import { ConfigError, loadConfig } from "../src/config.js";

const CONTRACTS_DIR = path.join(FIXTURES_DIR, "..");

// ------------------------------------------------------------------------------------- a fake analysis service
interface Seen { method: string; url: string; headers: http.IncomingHttpHeaders; body: string }
type Handler = (seen: Seen, res: http.ServerResponse, n: number, req: http.IncomingMessage) => void | Promise<void>;
interface Fake { url: string; seen: Seen[]; handler: Handler; close(): Promise<void> }
const open: Fake[] = [];

async function fake(handler: Handler): Promise<Fake> {
  const seen: Seen[] = [];
  const f: Fake = { url: "", seen, handler, close: async () => undefined };
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      const s = { method: req.method ?? "", url: req.url ?? "", headers: req.headers, body };
      seen.push(s);
      try {
        await f.handler(s, res, seen.length, req);
      } catch {
        if (!res.headersSent) res.writeHead(500).end("boom");
      }
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  f.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  f.close = () => new Promise((r) => { server.closeAllConnections(); server.close(() => r()); });
  open.push(f);
  return f;
}
afterEach(async () => { await Promise.all(open.splice(0).map((f) => f.close())); });

const json = (res: http.ServerResponse, status: number, body: unknown) => {
  res.writeHead(status, { "content-type": "application/json" }).end(typeof body === "string" ? body : JSON.stringify(body));
};

function client(url: string, over: Partial<ConstructorParameters<typeof HttpAnalysisClient>[0]> = {}) {
  return new HttpAnalysisClient({
    baseUrl: url, timeoutMs: 2000, retryDelayMs: 0, validate: createAnalyzeValidator(CONTRACTS_DIR), ...over,
  });
}
const recorded = (id: string) => fixtureJson<AnalyzeResponse>(`analyze/${id}.json`);

// ------------------------------------------------------------------------------------- the client on its own
describe("HttpAnalysisClient: the contract", () => {
  it("POSTs {docId} as JSON to /analyze and returns the validated response", async () => {
    const want = await recorded("D-02");
    const f = await fake((_s, res) => json(res, 200, want));
    expect(await client(f.url).analyze("D-02")).toEqual(want);
    expect(f.seen).toHaveLength(1);
    expect(f.seen[0]).toMatchObject({ method: "POST", url: "/analyze" });
    expect(f.seen[0]!.headers["content-type"]).toBe("application/json");
    expect(f.seen[0]!.body).toBe('{"docId":"D-02"}'); // nothing else: the service rejects extra keys
  });

  it("accepts every recorded fixture response and the contract's own example", async () => {
    const ids = (await readdir(path.join(FIXTURES_DIR, "analyze"))).map((n) => n.replace(".json", ""));
    expect(ids.length).toBeGreaterThanOrEqual(6);
    for (const id of ids) {
      const want = await recorded(id);
      const f = await fake((_s, res) => json(res, 200, want));
      expect(await client(f.url).analyze(id)).toEqual(want);
    }
    const sample = await fixtureJson<AnalyzeResponse>("../examples/analyze-response.sample.json");
    const f = await fake((_s, res) => json(res, 200, sample));
    expect((await client(f.url).analyze(sample.docId)).docId).toBe(sample.docId);
  });

  it("a trailing slash in the base URL does not matter", async () => {
    const f = await fake((_s, res) => json(res, 200, { /* replaced below */ } as unknown as object));
    f.handler = async (_s, res) => json(res, 200, await recorded("D-01"));
    await client(`${f.url}`).analyze("D-01");
    expect(f.seen[0]!.url).toBe("/analyze");
  });
});

describe("HttpAnalysisClient: a bad response is rejected, never used", () => {
  const mutate = async (fn: (r: Record<string, unknown>) => void) => {
    const r = JSON.parse(JSON.stringify(await recorded("D-02"))) as Record<string, unknown>;
    fn(r);
    return r;
  };
  const rejected = async (body: unknown, status = 200) => {
    const f = await fake((_s, res) => json(res, status, body));
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(f.seen).toHaveLength(1); // a bad answer is not retried: asking again cannot fix it
    return err as Error;
  };

  const SCHEMA = /does not match contracts\/analyze\.schema\.json: /;
  const RANGES = /breaks the value ranges the contract documents: /;
  const field = (r: Record<string, unknown>) => (r["fields"] as Record<string, unknown>[])[0]!;
  const signal = (r: Record<string, unknown>) => (r["signals"] as Record<string, unknown>[])[0]!;
  it.each([
    ["a missing required key", (r: Record<string, unknown>) => void delete r["pages"], SCHEMA],
    ["a wrong type", (r: Record<string, unknown>) => void (r["fields"] = "none"), SCHEMA],
    ["an unknown document type", (r: Record<string, unknown>) => void ((r["pages"] as Record<string, unknown>[])[0]!["docType"] = "passport"), SCHEMA],
    ["an unknown analyzer status", (r: Record<string, unknown>) => void ((r["analyzerStatuses"] as Record<string, unknown>)["ocr"] = "CLEAN"), SCHEMA],
    // the generated schema does not enforce these documented ranges, so the client does
    ["a signal confidence above 1", (r: Record<string, unknown>) => void (signal(r)["confidence"] = 7), RANGES],
    ["a negative OCR confidence", (r: Record<string, unknown>) => void (field(r)["ocrConfidence"] = -0.1), RANGES],
    ["a box outside 0..1", (r: Record<string, unknown>) => void ((field(r)["region"] as Record<string, unknown>)["bbox"] = [2, 0, 1, 1]), RANGES],
    ["a box that runs past the page edge", (r: Record<string, unknown>) => void ((field(r)["region"] as Record<string, unknown>)["bbox"] = [0.9, 0.1, 0.3, 0.1]), RANGES],
    ["a region on a page the document does not have", (r: Record<string, unknown>) => void ((field(r)["region"] as Record<string, unknown>)["page"] = 9), RANGES],
    ["a region that belongs to another document", (r: Record<string, unknown>) => void ((field(r)["region"] as Record<string, unknown>)["docId"] = "D-99"), RANGES],
  ])("%s", async (_name, fn, message) => {
    const err = await rejected(await mutate(fn));
    expect(err).toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(message);
  });

  it("not JSON, an empty body, or JSON of the wrong shape", async () => {
    for (const body of ["<html>oops</html>", "", "null", "[]", '"text"']) {
      const err = await rejected(body);
      expect(err).toBeInstanceOf(PermanentAnalysisError);
    }
    expect((await rejected("<html>oops</html>")).message).toMatch(/not JSON/);
  });

  it("an answer about a different document", async () => {
    const err = await rejected(await recorded("D-01")); // asked about D-02
    expect(err).toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toContain("answered for D-01 when asked about D-02");
  });
});

describe("HttpAnalysisClient: HTTP statuses", () => {
  it.each([
    [404, '{"detail":"document not found"}', /same DATA_DIR/],
    [409, '{"detail":"more than one original for X"}', /more than one original/],
    [415, '{"detail":"encrypted PDF"}', /encrypted PDF/],
    [422, '{"detail":[{"msg":"docId must match [A-Za-z0-9_-]+"}]}', /docId must match/],
    [400, "bad request", /HTTP 400/],
    [403, "no", /HTTP 403/],
  ])("%i is a permanent error with the service's own reason, and is not retried", async (status, body, message) => {
    const f = await fake((_s, res) => json(res, status, body));
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(err).toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(message);
    expect(f.seen).toHaveLength(1);
  });

  it("500 is a transient error (the job retries), not retried inside the client", async () => {
    const f = await fake((_s, res) => json(res, 500, '{"detail":"render crashed"}'));
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(err).not.toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(/failed \(HTTP 500\): render crashed/);
    expect(f.seen).toHaveLength(1);
  });

  it.each([502, 503, 504])("%i is retried once and then succeeds", async (status) => {
    const want = await recorded("D-02");
    const f = await fake((_s, res, n) => (n === 1 ? json(res, status, "not ready") : json(res, 200, want)));
    expect(await client(f.url).analyze("D-02")).toEqual(want);
    expect(f.seen).toHaveLength(2);
  });

  it("503 twice gives up after exactly one retry, as a transient error", async () => {
    const f = await fake((_s, res) => json(res, 503, "still not ready"));
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(err).not.toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(/HTTP 503/);
    expect(f.seen).toHaveLength(2);
  });

  it("waits retryDelayMs before the retry", async () => {
    const f = await fake((_s, res, n) => (n === 1 ? json(res, 503, "x") : void recorded("D-02").then((r) => json(res, 200, r))));
    const t0 = Date.now();
    await client(f.url, { retryDelayMs: 150 }).analyze("D-02");
    expect(Date.now() - t0).toBeGreaterThanOrEqual(140);
  });
});

describe("HttpAnalysisClient: network errors, timeouts, cancellation", () => {
  it("a dropped connection is retried once and then succeeds", async () => {
    const want = await recorded("D-02");
    const f = await fake((_s, res, n, req) => (n === 1 ? void req.socket.destroy() : json(res, 200, want)));
    expect(await client(f.url).analyze("D-02")).toEqual(want);
    expect(f.seen).toHaveLength(2);
  });

  it("two dropped connections: gives up with a transient 'could not be reached' error", async () => {
    const f = await fake((_s, _res, _n, req) => void req.socket.destroy());
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(err).not.toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(/could not be reached at http/);
    expect(f.seen).toHaveLength(2);
  });

  it("nothing listening: refused connection, one retry, transient error naming the cause", async () => {
    const f = await fake(() => undefined);
    const url = f.url;
    await f.close();
    const err = await client(url).analyze("D-02").catch((e) => e);
    expect(err).not.toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(/could not be reached/);
    expect(err.message).toMatch(/ECONNREFUSED/);
  });

  it("no answer within the timeout: AnalysisTimeoutError, not retried, and quick", async () => {
    const f = await fake(() => undefined); // never answers
    const t0 = Date.now();
    const err = await client(f.url, { timeoutMs: 80 }).analyze("D-02").catch((e) => e);
    expect(err).toBeInstanceOf(AnalysisTimeoutError);
    expect(err.message).toMatch(/within 80 ms/);
    expect(f.seen).toHaveLength(1);
    expect(Date.now() - t0).toBeLessThan(1500);
  });

  it("an answer that starts and then stalls also times out", async () => {
    const f = await fake((_s, res) => void res.writeHead(200, { "content-type": "application/json" }).write('{"docId":"D-0'));
    const err = await client(f.url, { timeoutMs: 100 }).analyze("D-02").catch((e) => e);
    expect(err).toBeInstanceOf(AnalysisTimeoutError);
  });

  it("an answer cut off halfway is a transient error", async () => {
    const f = await fake((_s, res, _n, req) => {
      res.writeHead(200, { "content-type": "application/json", "content-length": "5000" });
      res.write("{");
      setTimeout(() => req.socket.destroy(), 20);
    });
    const err = await client(f.url).analyze("D-02").catch((e) => e);
    expect(err).not.toBeInstanceOf(PermanentAnalysisError);
    expect(err.message).toMatch(/connection to the analysis service was lost/);
  });

  it("when the caller cancels (the job timeout), the request is aborted and not retried", async () => {
    const f = await fake(() => undefined);
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 40);
    const err = await client(f.url, { timeoutMs: 5000 }).analyze("D-02", { signal: ctrl.signal }).catch((e) => e);
    expect(err).not.toBeInstanceOf(AnalysisTimeoutError);
    expect((err as Error).name).toBe("AbortError");
    expect(f.seen).toHaveLength(1);
  });

  it("an already-cancelled call makes no request at all", async () => {
    const f = await fake((_s, res) => json(res, 200, {}));
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(client(f.url).analyze("D-02", { signal: ctrl.signal })).rejects.toThrow();
    expect(f.seen).toHaveLength(0);
  });

  it("ping reports whether the service answers /health, and never throws", async () => {
    const f = await fake((s, res) => (s.url === "/health" ? json(res, 200, { status: "ok", analyzersRegistered: 0 }) : json(res, 404, "")));
    expect(await client(f.url).ping()).toMatchObject({ ok: true });
    const url = f.url;
    await f.close();
    expect(await client(url).ping()).toMatchObject({ ok: false });
  });
});

describe("configuration", () => {
  const hash = `scrypt$${"a".repeat(32)}$${"b".repeat(128)}`;
  const cfg = (env: Record<string, string>) => loadConfig({ ANALYST_PASSWORD_HASH: hash, ...env });

  it("http mode: defaults, and a trailing slash on the URL is removed", () => {
    expect(cfg({ ANALYSIS_MODE: "http" })).toMatchObject({ analysisMode: "http", analysisUrl: "http://127.0.0.1:8000", analysisTimeoutMs: 110000 });
    expect(cfg({ ANALYSIS_MODE: "http", ANALYSIS_URL: "http://svc:9000///" }).analysisUrl).toBe("http://svc:9000");
    expect(cfg({}).analysisMode).toBe("mock");
  });
  it("rejects an invalid URL, the stub with the real service, and a timeout the job would pre-empt", () => {
    expect(() => cfg({ ANALYSIS_MODE: "http", ANALYSIS_URL: "not a url" })).toThrow(ConfigError);
    expect(() => cfg({ ANALYSIS_MODE: "http", REASONING_MODE: "stub" })).toThrow(/only works with ANALYSIS_MODE=mock/);
    expect(() => cfg({ ANALYSIS_MODE: "http", ANALYSIS_TIMEOUT_MS: "200000" })).toThrow(/smaller than JOB_TIMEOUT_MS/);
    expect(() => cfg({ ANALYSIS_TIMEOUT_MS: "200000" })).not.toThrow(); // only matters for the real service
  });
  it("a missing contracts folder fails loudly and says which setting to change", () => {
    expect(() => createAnalyzeValidator("/nonexistent")).toThrow(/CONTRACTS_DIR/);
  });
});

// ------------------------------------------------------------------------------------- through the whole backend
describe("the backend with ANALYSIS_MODE=http", () => {
  /** A fake service whose answers come from the recorded fixtures (matched by file hash, like the mock client). */
  async function backend(over: Record<string, string> = {}) {
    let delegate: (docId: string) => Promise<AnalyzeResponse> = async () => { throw new Error("not ready"); };
    const svc = await fake(async (s, res) => {
      const docId = (JSON.parse(s.body) as { docId: string }).docId;
      json(res, 200, await delegate(docId));
    });
    const t = await testApp({
      ANALYSIS_MODE: "http", ANALYSIS_URL: svc.url, ANALYSIS_RETRY_DELAY_MS: "0", ANALYSIS_TIMEOUT_MS: "2000",
      JOB_TIMEOUT_MS: "5000", JOB_MAX_ATTEMPTS: "2", CONTRACTS_DIR, ...over,
    });
    const mock = new MockAnalysisClient({ fixturesDir: FIXTURES_DIR, manifest: t.deps.manifest, storage: t.deps.storage });
    delegate = (docId) => mock.analyze(docId);
    const agent = await loggedInAgent(t.app);
    await agent.post("/api/v1/cases").send({ title: "http mode" }).expect(201);
    return { ...t, svc, agent, setDelegate: (d: typeof delegate) => { delegate = d; }, mock };
  }
  const jobOf = async (agent: Awaited<ReturnType<typeof loggedInAgent>>, id: string) => (await agent.get(`/api/v1/jobs/${id}`).expect(200)).body as Job;

  it("is wired to the real client", async () => {
    const t = await backend();
    expect(t.deps.analysisClient).toBeInstanceOf(HttpAnalysisClient);
  });

  it("upload, analysis over HTTP, reasoning and reads all work end to end", async () => {
    const t = await backend();
    const mapping = await uploadFixtureDocs(t.deps, t.agent, "CASE-001", ["D-01.png", "D-02.png", "D-03.pdf"]);
    expect(t.svc.seen.map((s) => s.url)).toEqual(["/analyze", "/analyze", "/analyze"]);
    expect(t.svc.seen.map((s) => (JSON.parse(s.body) as { docId: string }).docId)).toEqual(Object.values(mapping));

    const summary = (await t.agent.get("/api/v1/cases/CASE-001").expect(200)).body as CaseSummary;
    expect(summary.jobs.map((j) => j.status)).toEqual(["DONE", "PARTIAL", "DONE"]);
    expect(summary.priority).toMatchObject({ coverage: 0.667, incompleteCoverage: true });
    const findings = (await t.agent.get("/api/v1/cases/CASE-001/findings").expect(200)).body;
    expect(findings.findings.map((f: { finding: { type: string } }) => f.finding.type)).toContain("DOB_CONFLICT");
    const detail = (await t.agent.get(`/api/v1/documents/${mapping["D-01"]}`).expect(200)).body as DocumentDetail;
    expect(detail.pages[0]!.docId).toBe(mapping["D-01"]);
    await t.agent.get(`/api/v1/documents/${mapping["D-01"]}/pages/1/render`).expect(200);
  });

  it("an analyzer that failed inside the service makes the job PARTIAL, visibly, and the dossier lists it", async () => {
    const t = await backend();
    t.setDelegate(async (docId) => {
      const r = await t.mock.analyze(docId);
      if (r.analyzerStatuses["image.ela"] === "AVAILABLE") {
        r.analyzerStatuses["image.ela"] = "ERROR";
        r.provenance = r.provenance.map((p) => (p.analyzer === "image.ela" ? { ...p, status: "ERROR" as const, config: { ...p.config, error: "model file missing" } } : p));
      }
      return r;
    });
    await uploadFixtureDocs(t.deps, t.agent, "CASE-001", ["D-01.png"]);
    const [job] = (await t.deps.jobs.list());
    expect(job!.status).toBe("PARTIAL");
    expect(job!.analyzers["image.ela"]).toMatchObject({ state: "ERROR", analyzerStatus: "ERROR", message: "model file missing" });
    const dossier = (await t.agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200)).body as DossierJson;
    expect(dossier.limitations.some((l) => l.includes("image.ela") && l.includes("ERROR") && l.includes("model file missing"))).toBe(true);
  });

  it("a service that analysed nothing (only rendered pages, like O1 with no analyzers) is FAILED and not clean", async () => {
    const t = await backend();
    t.setDelegate(async (docId) => ({ ...(await t.mock.analyze(docId)), fields: [], signals: [], analyzerStatuses: {}, provenance: [] }));
    const mapping = await uploadFixtureDocs(t.deps, t.agent, "CASE-001", ["D-01.png", "D-02.png"]);
    for (const j of await t.deps.jobs.list()) {
      expect(j.status).toBe("FAILED");
      expect(j.error).toBe("the analysis service reported no analyzers, so nothing was analysed");
    }
    // the rendered pages are still there for the viewer
    const detail = (await t.agent.get(`/api/v1/documents/${mapping["D-01"]}`).expect(200)).body as DocumentDetail;
    expect(detail.pages).toHaveLength(1);
    // and reasoning treats those documents as failed: no findings, coverage 0, and the dossier says why
    const summary = (await t.agent.get("/api/v1/cases/CASE-001").expect(200)).body as CaseSummary;
    expect(summary.priority!.coverage).toBe(0);
    const d = (await t.agent.get("/api/v1/cases/CASE-001/dossier?format=json").expect(200)).body as DossierJson;
    expect(d.limitations.filter((l) => /analysis failed/.test(l))).toHaveLength(2);
    expect(d.findings).toEqual([]);
  });

  it("a service that is down: the job is retried, then FAILED with the reason, and the worker keeps going", async () => {
    const t = await backend();
    await t.svc.close();
    const up = (await t.agent.post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(path.join(FIXTURES_DIR, "originals", "D-01.png")), "a.png").expect(202)).body;
    await drain(t.deps);
    const job = await jobOf(t.agent, up.jobs[0].jobId);
    expect(job).toMatchObject({ status: "FAILED", attempts: 2, maxAttempts: 2 });
    expect(job.error).toMatch(/could not be reached/);
  });

  it("a service that cannot find the file (DATA_DIR not shared) fails at once, with the hint", async () => {
    const t = await backend();
    t.svc.handler = (_s, res) => json(res, 404, '{"detail":"document not found"}');
    const up = (await t.agent.post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(path.join(FIXTURES_DIR, "originals", "D-01.png")), "a.png").expect(202)).body;
    await drain(t.deps);
    const job = await jobOf(t.agent, up.jobs[0].jobId);
    expect(job).toMatchObject({ status: "FAILED", attempts: 1 });
    expect(job.error).toMatch(/same DATA_DIR/);
  });

  it("a response that breaks the contract is rejected: job FAILED at once, nothing stored", async () => {
    const t = await backend();
    t.setDelegate(async (docId) => ({ ...(await t.mock.analyze(docId)), pages: "none" }) as unknown as AnalyzeResponse);
    const up = (await t.agent.post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(path.join(FIXTURES_DIR, "originals", "D-01.png")), "a.png").expect(202)).body;
    await drain(t.deps);
    const job = await jobOf(t.agent, up.jobs[0].jobId);
    expect(job).toMatchObject({ status: "FAILED", attempts: 1 });
    expect(job.error).toMatch(/does not match contracts\/analyze\.schema\.json/);
    expect(await t.deps.analysis.get(up.documents[0].docId)).toBeUndefined();
  });

  it("a service that hiccups once (503) is retried inside the client and the job is DONE on its first attempt", async () => {
    const t = await backend();
    let first = true;
    t.svc.handler = async (s, res) => {
      if (first) { first = false; return json(res, 503, "warming up"); }
      json(res, 200, await t.mock.analyze((JSON.parse(s.body) as { docId: string }).docId));
    };
    const up = (await t.agent.post("/api/v1/cases/CASE-001/documents")
      .attach("files", (await import("node:fs")).readFileSync(path.join(FIXTURES_DIR, "originals", "D-01.png")), "a.png").expect(202)).body;
    await drain(t.deps);
    expect(await jobOf(t.agent, up.jobs[0].jobId)).toMatchObject({ status: "DONE", attempts: 1 });
  });
});
