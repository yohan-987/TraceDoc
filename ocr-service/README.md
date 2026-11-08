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

`DATA_DIR` (default `../data`), `RENDER_DPI` (150), `ANALYZER_TIMEOUT_S` (30), `MAX_PAGES` (100), `MAX_IMAGE_PIXELS` (50000000; larger images or PDF pages at the render DPI are rejected with 415),
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

## OCR (batch O2)

The `ocr` analyzer (`app/analyzers/ocr_tesseract.py`, core in `app/ocr_core.py` and `app/ocr.py`) runs
Tesseract on each canonical page PNG and stores the shared-contract `OcrPage` (words + lines, boxes
normalized to `[x, y, w, h]` in 0..1, confidence 0..1, 1-based pages) in the analysis context. It adds no
fields or signals to the response: `/analyze` shows only its status and provenance
(`config`: `lang`, `pages`, `pagesOk`, `words`, `lines`, optional `pagesFailed`/`reason`).

### Install Tesseract (separate from `pip`)

`pytesseract` is only a wrapper. You also need the native **Tesseract 5** program and the **English language
data** (`eng.traineddata`, included in the standard Windows installer; keep the "English" language selected).

* Windows installer: https://github.com/UB-Mannheim/tesseract/wiki
* Tell the service where it is, in order of precedence: `TESSERACT_CMD` env var / `.env`
  -> `tesseract` on `PATH` -> the standard `C:\Program Files\Tesseract-OCR\tesseract.exe` location.

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

If Tesseract or the requested language is missing, the analyzer reports **`UNAVAILABLE`** (reason in
`provenance[].config.reason`); `/analyze` still returns 200 with the rendered pages.

### Settings

`TESSERACT_CMD`, `OCR_LANG` (default `eng`), `OCR_PAGE_TIMEOUT_S` (default 25; per-page Tesseract limit).
The O1 `ANALYZER_TIMEOUT_S` (default 30) still bounds the whole OCR analyzer, i.e. **all pages together**:
raise it for long PDFs (about 1-3 s per page at 150 dpi).

### Behaviour

* Status `AVAILABLE`: every page was OCRed (a page with no text gives empty words/lines and the reason
  "no text detected"; that is not a statement that the page is clean or empty of content).
* Status `ERROR`: at least one page failed (corrupt render, Tesseract error/timeout, budget exhausted);
  `pagesFailed` lists them. Pages that did succeed stay available in the context.
* Reusing OCR in another analyzer: `from app.ocr import get_page_ocr` then `get_page_ocr(ctx, page_number)`.
  The first call runs Tesseract, later calls (any analyzer) get the cached `OcrPage`; failures are cached too.
  It raises `OcrUnavailableError` or `OcrPageError`, which the analyzer should map to a status.

### Tests

`tests/test_ocr_core.py` (parsing, no Tesseract) and `tests/test_ocr_analyzer.py` (Tesseract **mocked**)
always run. `tests/test_ocr_real.py` calls the real Tesseract and is skipped automatically if it is not found.

### Limitations

English only; no preprocessing (deskew, binarization); pages are OCRed sequentially; confidence is an
engine estimate, not accuracy. See the PaddleOCR and text-layer notes below for their own limits.

## OCR engines: PaddleOCR primary, Tesseract fallback (batch O2, part 2)

`OCR_ENGINE` selects the policy: `auto` (default) tries **PaddleOCR first** and uses **Tesseract** when PaddleOCR
is not installed, cannot initialise, or fails on a page; `tesseract` never loads PaddleOCR; `paddleocr` forbids
the fallback (a failure then shows as `UNAVAILABLE` / `ERROR`). `OcrPage.engine` / `engineVersion` record which
engine produced each page, and the analyzer's provenance lists them (`config.engines`, `modelVersion`).

* PaddleOCR is created **once per process** and reused for every page and request (inference is serialized).
  The first use loads (and, on first ever use, downloads) models, which can exceed `ANALYZER_TIMEOUT_S`. Either
  send one warm-up request, raise the timeout for it, or set `OCR_WARMUP=1` to initialise in the background at startup.
* If PaddleOCR fails on a page, it is skipped for the rest of that request and Tesseract handles the remaining pages.
* Install (optional, heavy): `python -m pip install -r requirements-paddle.txt`. Whether PaddlePaddle ships wheels
  for your Python version/Windows is outside this service; if the install fails the service runs on Tesseract.
* **PaddleOCR returns text lines, not words.** Word boxes are estimated by splitting each line on whitespace and
  sharing the line box by character count; every word gets its line's score. Treat Paddle word boxes as approximate.
  Supports the PaddleOCR 3.x (`predict`) and 2.x (`ocr`) APIs. `PADDLE_LANG` (default `en`).
* An unreadable page image is reported as a page error; it is not retried with the other engine.

## PDF text layer (`source = pdf_text_layer`)

`app/textlayer.py` reads the embedded text of digital PDFs with PyMuPDF (`get_text("words")`, page rotation applied),
normalizes boxes `[x, y, w, h]` (0..1) against the canonical render, and caches the whole document in `ctx` once per
request. Use `get_text_layer_page(ctx, page)` (returns `TextLayerPage(source="pdf_text_layer", page=OcrPage)`).

* It is **never merged into or substituted for OCR**; `get_page_ocr` and `get_text_layer_page` are separate and the
  source is explicit. The shared `OcrPage`/`OcrWord` models have no `source` field, so the source lives on the wrapper
  and in `page.engine == "pymupdf-text-layer"`.
* Text-layer words have no recognition confidence; `ocrConfidence` is **1.0**, meaning only "exactly the text encoded
  in the PDF". It does not mean the visible page shows that text (hidden or overlaid text is possible).
* The `pdf.text_layer_words` analyzer reports status/provenance only: `NOT_APPLICABLE` for images, `AVAILABLE` with
  `reason: "PDF has no text layer"` for scans (which says nothing about authenticity).

## Debug endpoint (development only)

`GET /debug/ocr/{docId}/{page}[?source=ocr|pdf_text_layer]` returns the canonical page PNG with boxes drawn
(blue = line; words green / orange / red by confidence >= 0.8 / 0.5-0.8 / < 0.5; magenta = text-layer words).
Pages are 1-based. It exists **only** when `DEBUG_ENDPOINTS=1` (default off; otherwise the route is absent and
returns 404). Errors: 422 invalid docId/page/source, 404 unknown document, page or text layer, 503 no OCR engine,
502 OCR failed on the page. It takes no paths and only serves renders of the given document. It is unauthenticated:
keep the service bound to `127.0.0.1` and never enable it in production.

```powershell
$env:DEBUG_ENDPOINTS = "1"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000/debug/ocr/CASE-001-DOC-01/1 in a browser
```

## Settings added in this batch

`OCR_ENGINE` (`auto`), `PADDLE_LANG` (`en`), `OCR_WARMUP` (off), `DEBUG_ENDPOINTS` (off). See `.env.example`.

## Licensing addendum

PaddleOCR / PaddlePaddle are Apache-2.0. PyMuPDF (used for rendering and the text layer) remains **AGPL-3.0**, see above.

## Input limits and hardening

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_IMAGE_PIXELS` | 50,000,000 | largest single page / image; bigger input is rejected with 415 before it is decoded |
| `MAX_TOTAL_PIXELS` | 400,000,000 | all pages of one document together (bounds time and disk use) |
| `ANALYZER_TIMEOUT_S` | 30 | must be finite and in (0, 3600]; NaN/inf are refused at startup |

* `docId` is limited to 128 characters (422 above that).
* A multi-page TIFF renders one page per frame. Other multi-frame formats (GIF, APNG, WebP, MPO) render their first frame and log a warning.
* The render is written through a unique temp file, so concurrent analyses of one document are safe.
* The analysis context locks per key, so one slow computation does not stall unrelated ones.
