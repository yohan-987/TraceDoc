# backend/

Express + TypeScript API for TraceDocs (Person 2 owns everything here except `src/engine/`).
See `../ARCHITECTURE.md` and `../contracts/`.


## Run

```bash
cd backend
npm install
npm run hash-password -- "your password"   # paste the output into .env as ANALYST_PASSWORD_HASH
cp .env.example .env                       # then edit
npm run dev
```

The server reads real environment variables; load `.env` with your shell or `node --env-file=.env`.

## Test

```bash
npm test
npm run typecheck
```

## Notes

- Types come from `../contracts/` through the `@contracts/*` alias (tsconfig and vitest both configured).
- Single-analyst auth: scrypt password hash from env, random server-side session token in an HttpOnly,
  SameSite=Lax cookie. Sessions live in memory and end on restart. Failed logins are rate limited per IP.
- Every route except `/health` and `/auth/*` must be mounted behind `deps.requireAuth`.

## Storage and ingestion (B2)

- `POST /api/v1/cases/:caseId/documents` (multipart, field `files`, up to 20 files). The case must exist (404 otherwise).
  Responds `202 { caseId, documents, jobs }` as in `contracts/openapi.yaml`.
- File type is decided from magic bytes (PDF, PNG, JPEG, TIFF). The client's extension and Content-Type are ignored.
  One bad file rejects the whole request with 400 and nothing is stored. Oversized files also give 400 (`FILE_TOO_LARGE`).
- Originals go to `DATA_DIR/evidence/<docId>/original.<ext>`, opened with the `wx` flag (the OS refuses an
  overwrite) and set read-only. Paths are built only from the server-generated docId, never from the filename.
- `DATA_DIR/audit/manifest.json` is an append-only array of `{docId, caseId, filename, sha256, bytes, mimeType, storedAs, receivedAt}`.
  `caseId`, `mimeType` and `storedAs` are additions beyond the minimum so B12 can re-verify without guessing paths.
- Derivatives belong in `DATA_DIR/derived/<docId>/` (`Storage.derivedDir`).

## Cases, documents and jobs (B3)

- Routes: `POST/GET /cases`, `GET /cases/:id`, `GET /documents/:docId`, `GET /jobs/:jobId` (all behind login).
  Case ids are sequential (`CASE-001`, ...), job ids too (`JOB-0001`, ...).
- Records are JSON files under `DATA_DIR/records/{cases,documents,jobs,analysis}/` behind repository interfaces
  (`src/repo/repositories.ts`), written via temp file + rename.
- A job has states QUEUED / RUNNING / PARTIAL / DONE / FAILED and per-analyzer progress.
  - Idempotent by `(docId, pipelineVersion)`: enqueueing again returns the existing job. A forced re-run is a new job.
  - `attempts` counts runs started; a failed run is re-queued until `JOB_MAX_ATTEMPTS`, then FAILED. A permanent error
    (for example no recorded analysis) fails at once.
  - Some analyzers ERROR/UNAVAILABLE and some AVAILABLE gives PARTIAL; none AVAILABLE gives FAILED. Never reported as clean.
  - On startup `recoverRunning()` moves jobs left RUNNING by a dead process back to QUEUED (FAILED if they used every attempt).
- `AnalysisClient` is the only view of the analysis service (`src/services/analysisClient.ts`). `ANALYSIS_MODE=mock` uses
  `MockAnalysisClient`: it matches the uploaded file's SHA-256 to a document in `contracts/fixtures/CASE-*.case.json`
  (upload files from `contracts/fixtures/originals/`) and returns the recorded `analyze/<docId>.json`, re-labelled with the
  server-generated docId. Any other file fails with a clear message. The real HTTP client arrives in B11.
- The worker (`src/services/worker.ts`) runs one job at a time with a timeout (`JOB_TIMEOUT_MS`) and tells listeners when a job
  finishes (B10 hooks reasoning in there). It starts from `server.ts`; tests call `worker.processNext()` directly.

## Evidence store and read APIs (B8)

- `src/repo/evidence.ts`: findings, evidence, assertions, entities, region events, signals and priority of a case are stored as
  **generations** under `DATA_DIR/records/evidence/<caseId>/gen-0001.json`, `gen-0002.json`, ... A generation is written once
  (temp file, then a hard link that fails if the target exists), made read-only, and never modified. A re-run (B10 recompute) writes
  the next generation and keeps the old ones. The API shows the latest generation; older finding ids stay readable.
- **Finding ids are namespaced** when stored: the engine's `F-0001` becomes `<caseId>-g<generation>-F-0001`
  (e.g. `CASE-001-g1-F-0001`). Engine ids repeat across cases and re-runs, and `GET /findings/:findingId` has no case in its path.
  A new generation starts with fresh, unreviewed findings, and `priority.contributingFindingIds` is renamed to match.
  Everything else in a finding is stored exactly as the engine produced it.
- Routes (all behind login, shapes from `contracts/openapi.yaml`):
  `GET /cases/:id/findings` (filters `category`, `observationStatus`, `reviewStatus`; unknown values are 400),
  `GET /findings/:id`, `GET /cases/:id/assertions`, `GET /cases/:id/entities`, `GET /cases/:id/graph`,
  `GET /documents/:docId/pages/:n/render`.
  A case with no reasoning run yet returns empty lists and a `null` priority, never a "clean" result.
  `GET /cases/:id` now carries the stored `priority` and `perDocumentPriority`.
- Review status comes from a `ReviewSource` (`src/services/reviewSource.ts`). Until B9 it reports UNREVIEWED and no history.
- The graph (`src/services/graph.ts`) is derived from stored evidence: `contains` (entity to each of its documents), `match`
  (same `id_number` / `account_number` value for one entity across documents), `conflict` (INCONSISTENT findings that span two
  documents; labelled `(capped)` when `confidenceCapped` is set) and `suggested` (a SUGGESTED entity towards the entity named in
  its `basis`, e.g. "name similarity 100 to E-001"). It reproduces `contracts/fixtures/CASE-00x.graph.json` exactly.
- **Page renders** are served from `DATA_DIR/derived/<docId>/page-<n>.png`. The path is built from the validated docId and page
  number only; the `renderPath` string in an analysis response is never used to open a file. **The Python service must write its
  canonical renders to that location** (the shared `DATA_DIR`, same convention as `renderPath`). In mock mode the mock client copies
  the fixture page images there.
- `tsconfig.json` excludes `src/engine/`: it is its own package with its own tsconfig and tests (Person 1).

## Reasoning pipeline (B10)

- `src/services/reasoning.ts`. When a job finishes and **no job of that case is still QUEUED or RUNNING**, the case's stored analyses
  (`AnalyzeResponse` per document, in upload order) go to the engine and the result is stored as a new evidence generation.
  It is hooked to `Worker.onJobFinished`. Reasoning never re-analyses documents, and runs are serialized per case.
- `REASONING_MODE=engine` (default) uses the real engine, `runReasoning` from `src/engine`. `REASONING_MODE=stub` returns the recorded
  `contracts/fixtures/<case>.reasoning.json` re-labelled with the real document ids. It only knows the fixture cases and needs
  `ANALYSIS_MODE=mock` (documents are matched to fixtures by hash). An unknown case gives `409 NO_STUB_REASONING`.
- **A failed analysis is never read as clean.** A document whose analysis failed completely is still given to the engine, as a document
  whose analyzers all reported ERROR, so coverage drops (per document it is 0). Documents that are PARTIAL (an analyzer UNAVAILABLE or
  ERROR) lower coverage the same way, and coverage never changes the band. Confidence and coverage are reported separately in `priority`.
- If the engine throws, nothing is stored, the worker keeps going, the previous generation stays current, and a case that never had one
  keeps `priority: null`.
- `POST /api/v1/cases/:caseId/recompute` re-runs reasoning on the stored analyses and returns the refreshed `CaseSummary`.
  It writes a new generation (old findings stay readable). `409 NO_DOCUMENTS` if the case is empty, `409 ANALYSIS_PENDING` while jobs
  are still running, `404` for an unknown case. **This route is not in `contracts/openapi.yaml` yet** (it returns the existing
  `CaseSummary`); Person 1 owns that file.
- The graph's `suggested` edges now follow the structured `Entity.suggestedLinks` (best score first) and only fall back to the entity
  named in `basis` for records without it.
- `tsconfig.json` no longer excludes `src/engine`: the engine is type-checked with the same strict flags as the rest of the backend.

## Review workflow and investigation log (B9)

- **Event log** (`src/services/eventLog.ts`): `DATA_DIR/audit/events.jsonl`, one JSON event per line, append-only (no update, no delete).
  Ids are sequential (`IE-0001`...). A corrupt line stops the log from loading, so audit data is never silently dropped. The one tolerated
  damage is an unfinished last line from a crash: it is cut off the end and kept in `events.jsonl.discarded`.
- **Review** (`POST /findings/:findingId/review`, body `{action, reason?, comment?}`): `CONFIRM`, `INCONCLUSIVE` and `DISMISS` append a
  `REVIEW` event (`payload.status`), `COMMENT` appends a `COMMENT`, `REOPEN` appends a `REOPEN`. `DISMISS` needs a reason and `COMMENT` needs
  text (400). `REOPEN` on a finding that is not reviewed is 409. Returns the updated `FindingDetail`.
- **Review status is derived, never stored**: replay of the finding's events in order (`deriveReviewStatus`): REVIEW sets it, REOPEN
  returns it to UNREVIEWED, COMMENT and VIEW change nothing. The finding and the stored generation file are never modified, so a machine
  observation cannot be overwritten by a review. `reviewHistory` lists REVIEW, COMMENT and REOPEN events.
- Reviews attach to the namespaced finding id (B8). Only the **current** generation can be reviewed; an older one is 409 `STALE_GENERATION`
  but stays readable with the decisions made about it. After a recompute the new findings start UNREVIEWED.
- **Reclassify** (`PATCH /documents/:docId/pages/:n/type`, body `{docType, reason?}`): stores the analyst's correction, logs `RECLASSIFY`
  (`from`, `to`, `machineDocType`, `reason`), queues a forced re-run (202 `Job`) and logs `RERUN`; when the job finishes reasoning runs again.
  The correction lives in `DATA_DIR/records/overrides/` and is overlaid when analyses are read (`OverlayAnalysisRepository`), so every reader
  sees the analyst's type (confidence 1) while the stored machine output stays exactly as the analyzers produced it. Asking for the type a
  page already has is 400 `NO_CHANGE`. **Limit:** `AnalyzeRequest` has no field for a type hint, so the analysis service does not re-extract
  fields for the new type; the corrected type affects classification, roles and reasoning. Real re-extraction needs a contract field
  (for example `docTypeOverrides`) from Person 1.
- **Logging from the other routes**: `UPLOAD` per document (filename, sha256, jobId), `RERUN` on recompute (with the new generation) and on
  reclassify, `VIEW` when a finding (`GET /findings/:id`) or a document (`GET /documents/:docId`) is opened, at most once a minute per actor
  and target. Lists and page renders are not logged. `EXPORT` events are added by the dossier route (B14) through `deps.events.append`.
- `GET /cases/:caseId/events` returns the case's log in order (`EventsResponse`).

## Integrity verifier (B12)

- `GET /api/v1/cases/:caseId/integrity` returns an `IntegrityReport`. For every document of the case it recomputes the SHA-256 of the stored
  original (streamed) and compares it with `DATA_DIR/audit/manifest.json`, which B2 wrote at upload.
- A document **PASSES** only if its file exists and the recomputed hash equals the manifest's. Everything else is **FAIL**, never swallowed:
  a changed file (both hashes are returned), a missing or unreadable file (`actualSha256: null`), a document with no manifest entry, or an
  unreadable manifest (`expectedSha256: ""`; every document fails, and the response is still 200 with the report rather than a 500).
  The case is FAIL if any document fails. A case with no documents has nothing that failed, so it reports PASS with an empty list.
- The path of each file is rebuilt from the docId and an extension, never read from the manifest's `storedAs`, so a tampered manifest cannot
  make the check hash another file. The check is read-only: it never repairs or rewrites the manifest or the originals.
- `src/services/integrity.ts` (`IntegrityService`) is reusable: the dossier route (B14) embeds the same report.
- **Limit:** this detects changes made after upload. It cannot show that the manifest itself was never rewritten together with a file; for
  that the manifest would need an external anchor (for example a signed or off-machine copy), which is out of scope for the prototype.

## Dossier (B14)

- `GET /api/v1/cases/:caseId/dossier` returns a PDF (default, `Content-Disposition: attachment`); `?format=json` returns the raw `DossierJson`
  from `contracts/`. Both carry the same content, and each export appends an `EXPORT` event (format, generation, finding count, integrity result).
  Unknown `format` is 400, unknown case 404. Code: `src/reports/bundle.ts` (collects the data, computes the limitations),
  `src/reports/dossier.ts` (renders with `pdfkit`, MIT), `src/reports/service.ts`, `src/routes/dossier.ts`.
- Sections: **1** document overview (type, source, SHA-256, processing status), **2** extracted information (OCR and
  extraction confidence kept separate), **3** contextual analysis (entities, cross-document and context findings), **4** visual forensic findings
  (which checks ran, findings, region events and signals), **5** evidence and traceability (document, page and normalized bounding box for every
  region, plus a crop of the real page render with the box drawn in red), **6** investigation summary (anomalies, potential explanations, areas
  requiring verification, limitations and analyzer failures, integrity of the originals, review outcomes and history).
- **Observed and potential are never merged.** Every finding shows the machine's observation (for example "Inconsistency observed in extracted
  values" or "Potential anomaly (needs verification)") next to the analyst's review status. A review never changes the observation. The text avoids
  any wording about intent or responsibility.
- **Gaps are printed, not hidden.** `limitations` lists failed or unfinished analysis, analyzers that were UNAVAILABLE or ERROR ("results are missing,
  not clean"), a case on which reasoning never ran, coverage below 1, and integrity failures. A page render that cannot be read becomes a visible
  "Page image not available" box instead of a missing crop. An empty case and a case with no findings say so explicitly.
- **Potential explanations** use the finding's own `explanation` when it has one (B13). Otherwise general considerations for that kind of
  finding are printed once per kind and labelled as general, together with each rule's note from `config/comparability.json`.
- `reviewHistory` holds analyst decisions only (REVIEW, COMMENT, REOPEN, RECLASSIFY), in order, including decisions about earlier reasoning runs.
  VIEW, UPLOAD, RERUN and EXPORT stay in the investigation log (`GET /cases/:id/events`).
- The built-in PDF fonts cover Latin-1 and common punctuation; any other character is printed as `?` rather than corrupting the page.
  Crops are clipped from each page image, which is embedded once and reused (at most 8 crops per evidence item; the JSON lists all regions).
- Tests read the text of an uncompressed PDF (`compress: false`) and count the `Do` operators to check the crops, so no PDF-parsing library is needed.

## Real analysis service client (B11)

- `ANALYSIS_MODE=http` uses `HttpAnalysisClient` (`src/services/httpAnalysisClient.ts`): `POST {ANALYSIS_URL}/analyze` with `{"docId": ...}` (nothing
  else: the service rejects extra keys). The service reads the original from the **shared `DATA_DIR`**; the defaults of backend and service differ,
  so set it explicitly for both. Full details, the PowerShell run commands and the failure table are in `docs/ANALYSIS_INTEGRATION.md`.
- The response is validated against `contracts/analyze.schema.json` with ajv (now a runtime dependency), then checked for the value ranges the
  contract documents but the generated schema does not enforce (0..1 boxes and confidences, regions on an existing page of this document).
  A response that fails either check, or names another document, is a permanent failure and is never stored.
- Per-call timeout (`ANALYSIS_TIMEOUT_MS`), one retry after a dropped connection or HTTP 502/503/504 (`ANALYSIS_RETRY_DELAY_MS`), no retry after a
  timeout or a cancel. 404/409/415/422 and other 4xx are permanent and carry the service's own reason; 5xx is transient.
- Analyzers that fail inside the service arrive as `ERROR`/`UNAVAILABLE` in `analyzerStatuses`: the job becomes **PARTIAL** (all of them shown per
  job). If **no** analyzer succeeded, or the service reports no analyzers at all, the job is **FAILED**, never a clean DONE. Such a document goes to
  the reasoning engine as a failed document (coverage drops), and its rendered pages stay available.
- On startup the backend pings `GET /health` and logs whether the service is reachable; it does not refuse to start.
- `test/httpClient.test.ts` uses a fake service (contract fixtures, bad responses, every status, retries, timeouts, cancellation, and the whole
  backend end to end). `test/realService.test.ts` is opt-in (`REAL_ANALYSIS_URL`, `REAL_ANALYSIS_DATA_DIR`, see the docs) and was run against the O1
  service from this repo.
