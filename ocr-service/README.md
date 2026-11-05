# ocr-service (TraceDocs analysis service)

Python + FastAPI service owned by Person 3. Batch **O1**: scaffold, canonical page render, analyzer plugin
registry. It does **no** OCR, classification, extraction or forensic detection yet (later batches), so it never
reports a document as clean: with no analyzers it returns pages only, with empty `fields`, `signals`,
`analyzerStatuses` and `provenance`.

The shared contracts (`../contracts/`) are the source of truth. This service only reads them
(`analyze_models.py`, put on `sys.path` by `app/config.py`) and never modifies them.

## Licensing and dependencies

| Package | Purpose | License |
| --- | --- | --- |
| **PyMuPDF** | render PDF pages to PNG | **AGPL-3.0** (or Artifex commercial license) |
| Pillow | load/rotate images, write PNG | HPND (permissive) |
| FastAPI, Uvicorn, Pydantic, httpx, pytest | API, server, models, tests | MIT / BSD-3 |

> **PyMuPDF is AGPL-3.0.** If this service is distributed or offered to users over a network, AGPL obligations
> (offering the corresponding source of the combined work) can apply unless an Artifex commercial license is
> obtained. Decide this with the team before any deployment beyond local development. Keep PyMuPDF isolated in
> `app/render.py` so it can be swapped for another renderer.

## Run (PowerShell, Windows, Python 3.13)

```powershell
cd ocr-service
py -3.13 -m venv .venv
# If activation is blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:DATA_DIR = "..\data"      # optional; default is ../data (relative to ocr-service/)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Try it (an original must exist at `DATA_DIR\evidence\CASE-001-DOC-01\original.pdf` or `.png`/`.jpg`):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/analyze -ContentType "application/json" -Body '{"docId":"CASE-001-DOC-01"}' | ConvertTo-Json -Depth 8
```

## Tests

```powershell
cd ocr-service
python -m pytest -v
```

Tests use temporary directories and generated fictional documents only.

## Configuration (env vars, optional `.env`, see `.env.example`)

`DATA_DIR` (default `../data`), `RENDER_DPI` (150), `ANALYZER_TIMEOUT_S` (30), `MAX_PAGES` (100),
`CONTRACTS_DIR` (default `../contracts`).

## API

* `GET /health` -> `{"status":"ok", ...}` (service is running; says nothing about analyzer health).
* `POST /analyze` `{"docId": "..."}` -> `AnalyzeResponse`.
  * `docId` must match `[A-Za-z0-9_-]+`; extra keys (such as a path) are rejected: **422**.
  * Original is read only from `DATA_DIR/evidence/<docId>/original.<ext>`: missing **404**, more than one
    `original.*` **409**, unreadable/encrypted/unsupported **415**.

## Canonical render

* PDF -> PNG per page (PyMuPDF, `RENDER_DPI`); the page's `/Rotate` is applied and recorded as `rotationDeg`.
* Image -> EXIF-orientation-corrected, converted to RGB PNG; native pixels kept (no resampling), so `renderDpi`
  is the nominal configured DPI. Only the first frame of multi-frame images is used.
* Saved to `DATA_DIR/derived/<docId>/page-N.png` (1-based); stale higher-numbered pages from an earlier
  render are removed. `PageRecord.renderPath` is `derived/<docId>/page-N.png`, relative to `DATA_DIR`.
* `deskewDeg` is always `0.0` in O1 (no deskew estimation yet). `docType` is `UNKNOWN`, confidence `0.0`.
* Page width/height in pixels are kept on the internal `RenderedPage` (the contract's `PageRecord` has no such
  field).

## Analyzer plugins

Add `app/analyzers/<module>.py` with a class derived from `Analyzer` decorated with `@register_analyzer`
(`name`, `version`, `applies_to(page)`, `run(ctx) -> AnalyzerResult`); see `app/analyzers/__init__.py`.

* Each analyzer runs with `ANALYZER_TIMEOUT_S`. Exceptions, timeouts, invalid output or a failed plugin
  import give status `ERROR` (reason in that analyzer's `provenance[].config.reason`); other analyzers'
  output is kept.
* Only `AVAILABLE` results contribute fields/signals. `UNAVAILABLE`, `ERROR`, `NOT_APPLICABLE` are reported
  as-is, never as clean. If `applies_to` is false for every page the status is `NOT_APPLICABLE`.
* Share OCR through `ctx.get_ocr_page(page, compute)` / `ctx.get_or_compute(key, factory)` (computed once).

## Known limitations (O1)

* A timed-out analyzer thread cannot be killed in Python; its result is discarded but it may keep running
  until it returns. Hard isolation (subprocesses) is a candidate for O10.
* Analyzers run sequentially. No job store, caching or idempotency (that is the Node side / later batches).
* Mirrored EXIF orientations are corrected in the pixels but the mirror is not recorded.
