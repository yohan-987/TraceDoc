# TraceDocs

AI-assisted document forensics and contextual intelligence (hackathon prototype, built from scratch).

TraceDocs takes the related documents of one case (scanned or digital IDs, application forms, bank statements), keeps them as hashed evidence, extracts structured values with their locations, runs forensic and context-aware consistency checks, and produces an investigation dossier in which every finding traces back to a region of an original document and every analyst decision is recorded.

**Scope:** fictional or explicitly authorized documents only. Findings are observations or potential anomalies, never fraud verdicts, and they say nothing about intent. The output is an *investigation priority* for ordering analyst attention, not a probability of fraud.

## How it works

1. **Ingest.** The backend validates the upload by magic bytes, stores the original write-once, and records its SHA-256 in an append-only manifest.
2. **Analyse.** The Python analysis service renders every page to a canonical image, classifies it, runs OCR, extracts fields and tables, and runs forensic analyzers. Each analyzer reports its own status, and a failed or inapplicable one is `UNAVAILABLE`, `ERROR` or `NOT_APPLICABLE`, never "clean".
3. **Reason.** The reasoning engine turns fields into assertions, resolves entities conservatively, applies comparability and context rules (a difference only matters where the values should agree), groups overlapping forensic signals into events, and fuses them into a priority with a separate coverage figure.
4. **Review.** The analyst confirms, dismisses, comments or reopens findings. Reviews are appended to an event log; the machine observation is never overwritten.
5. **Report.** The dossier lists findings with page and bounding-box evidence, review history, limitations, and an integrity check of the stored originals.

See `ARCHITECTURE.md` for the full design and data model.

## Repository layout

| Folder | Purpose |
| --- | --- |
| `contracts/` | Shared types, JSON Schemas, pydantic mirror, fixtures, OpenAPI. Source of truth for all three services. |
| `backend/` | Node + Express + TypeScript API: ingestion, jobs, evidence store, review, dossier, integrity. The reasoning engine lives in `backend/src/engine/` |
| `ocr-service/` | Python + FastAPI analysis service: render, OCR, classification, extraction, layout, forensic analyzers |
| `frontend/` | React + Vite investigation UI |
| `samples/` | Fictional sample documents, tampered variants, dev/test cases and ground truth |
| `eval/` | Placeholder for the evaluation harness (not built yet) |
| `docs/` | Limitations, integration status, backend-to-service integration guide |

## Project state

`docs/INTEGRATION_STATUS.md` is the project's record of what is verified to work, where "verified" means a test in this repository exercises it. **It was last updated on 2026-11-04 and is behind the code**: it still lists the analysis service and the dossier as not built, while the component READMEs describe both. This README describes what the code contains and does not claim that every item below has been re-verified.

In the repository today (per the component READMEs and the source tree):

- **Backend:** upload and evidence store, persistent job queue and worker, the reasoning engine, evidence and review APIs, append-only event log, integrity verifier, related-case correlation, retention policy report, an HTTP client for the analysis service, and a server-side dossier (PDF or JSON).
- **Analysis service:** canonical page render, OCR (PaddleOCR optional, Tesseract fallback), page classifier, field extractor, bank-statement table reader, PDF text layer vs render check, and image forensics (copy-move, noise, ELA).
- **Frontend:** the investigation UI, in fixture mode by default and against the live backend for the routes the backend mounts.
- **Samples:** genuine documents, tampered documents with ground truth, and a dev/test split of 16 cases.

Not built: PDF font and revision checks, signature analysis in the analysis service (the samples generator can produce signature edits, but no analyzer checks them), the optional LLM explainer, the evaluation harness, and `docs/DEMO.md`. Photo-replacement detection, handwriting recognition and multilingual OCR are out of scope.

All measurements come from small synthetic sample sets. No claim of accuracy on real-world documents is made.

## Quick start

Prerequisites: Node 20 or newer. The analysis service additionally needs Python 3.13 (the version its docs target) and the native Tesseract 5 program with English data. PaddleOCR is optional and heavy.

### Frontend only (fixture mode)

Needs nothing else. Fixture mode is the default.

```bash
cd frontend
npm install
npm run dev
```

### Backend with recorded analysis (mock mode)

Mock mode is the default. The backend recognises the recorded fixture documents by file hash (upload files from `contracts/fixtures/originals/`) and fails any other file with an explicit reason.

```bash
cd backend
npm install
npm run hash-password -- "your password"   # paste the output into .env as ANALYST_PASSWORD_HASH
cp .env.example .env                       # then edit
npm run dev
```

The server reads real environment variables, so load `.env` with your shell or `node --env-file=.env`. To point the frontend at it, set `VITE_USE_MOCKS=false` in `frontend/.env.local` and sign in with the backend's `ANALYST_ID` and password. Details: `backend/README.md` and `frontend/README.md`.

### Full stack with real analysis

Set `ANALYSIS_MODE=http` for the backend and run the analysis service next to it. **Both processes must use the same absolute `DATA_DIR`**: the backend writes originals there and the service reads them. Their defaults differ, so set it explicitly for both. Step-by-step commands and the failure table are in `docs/ANALYSIS_INTEGRATION.md`; service setup, including Tesseract and PaddleOCR, is in `ocr-service/README.md`.

## Tests

| Component | Command |
| --- | --- |
| `contracts/` | `npm test` (needs `python3` and pydantic) |
| `backend/` | `npm test` and `npm run typecheck` |
| `frontend/` | `npm test`; `npm run test:e2e` after `npx playwright install chromium` |
| `ocr-service/` | `python -m pytest -v` (tests that need a real Tesseract skip themselves without one) |
| `samples/` | `python samples/validate.py` and `python -m pytest samples/tests -q` |

The reasoning engine in `backend/src/engine/` is its own package; see its README. The held-out `samples/test/` set is never used for tuning.

## Documentation

- `ARCHITECTURE.md`: design, data model, priority model, API surface
- `docs/INTEGRATION_STATUS.md`: verified-capability record (currently out of date, see Project state)
- `docs/LIMITATIONS.md`: what TraceDocs does not do, stated plainly (parts of it predate the analysis service)
- `docs/ANALYSIS_INTEGRATION.md`: how the backend and analysis service connect
- `contracts/README.md`: the shared contracts and how to change them
- `samples/README.md`: sample generation, ground truth and the engine check
- Per-component READMEs in `backend/`, `ocr-service/` and `frontend/`

## Limitations in brief

Single analyst account, in-memory sessions and file-based storage; retention is reported but nothing is purged; thresholds, weights and priority bands are untuned starting values; OCR and forensic detectors produce false positives and misses, and a low priority with incomplete coverage is not an all-clear. Full list in `docs/LIMITATIONS.md`.

## Licensing and third-party software

No license has been chosen for TraceDocs itself yet, so no permission to reuse the code is granted. Decide this before sharing or deploying it.

Notable dependencies and their licenses, as documented in the component READMEs:

| Component | Package | License |
| --- | --- | --- |
| `ocr-service/` | **PyMuPDF** (page rendering, PDF text layer) | **AGPL-3.0** (or Artifex commercial license) |
| `ocr-service/` | PaddleOCR / PaddlePaddle (optional) | Apache-2.0 |
| `ocr-service/` | Pillow | HPND |
| `ocr-service/` | FastAPI, Uvicorn, Pydantic, httpx, pytest | MIT / BSD-3 |
| `backend/` | pdfkit (dossier PDF) | MIT |
| `frontend/` | Cytoscape | MIT |
| `samples/` | Pillow (HPND), ReportLab (BSD), pypdfium2 (Apache-2.0/BSD), pikepdf (MPL-2.0), Faker (MIT) | as listed |
| `samples/` | Liberation fonts | SIL OFL (see `samples/generator/fonts/`) |

**PyMuPDF is AGPL-3.0.** If the analysis service is distributed or offered to users over a network, AGPL obligations can apply unless an Artifex commercial license is obtained. It is isolated in `ocr-service/app/render.py` so it can be swapped for another renderer. The backend talks to the service over HTTP only.

## AI assistance and acknowledgements

- AI coding assistants (Claude, from Anthropic) were used during development of this repository.
- Design ideas for risk fusion (noisy-OR with a cap on weak evidence), graceful degradation of optional layers, an access log, a retention policy endpoint and cross-record correlation come from the team's earlier email-forensics project, TraceMail. No TraceMail code is used here; everything in this repository was written for TraceDocs.
