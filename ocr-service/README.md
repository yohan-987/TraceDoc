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

### PaddleOCR troubleshooting

* **`NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`**
  is raised during inference, not at start-up, so PaddleOCR "initialises" fine and then fails. It is a oneDNN / PIR
  regression in PaddlePaddle 3.3.x on x86-64 CPUs (PaddlePaddle/Paddle#77340, PaddlePaddle/PaddleOCR#18162). The
  service therefore constructs PaddleOCR with `enable_mkldnn=False` (`PADDLE_ENABLE_MKLDNN=0`, the default). Setting
  `PADDLE_ENABLE_MKLDNN=1` brings the error back on affected versions. Reported alternative: pin `paddlepaddle==3.2.2`
  (not confirmed here to have a Python 3.13 Windows wheel).
* Pinned for this project in `requirements-paddle.txt`: `paddlepaddle==3.3.1`, `paddleocr>=3.7,<3.8` (PaddleX is
  pulled in by PaddleOCR). Python 3.13 / Windows x86-64 has a PaddlePaddle 3.3.1 wheel.
* Models cached under the user profile: if your Windows user folder contains non-ASCII characters and PaddleOCR cannot
  read its models (parse errors), point `PADDLE_PDX_CACHE_HOME` at an ASCII-only path.
* Run `python tools/paddle_check.py` for the installed versions plus a real inference on the three sample documents
  (oneDNN off vs on). A failure never crashes `/analyze`: the page is OCRed by Tesseract unless `OCR_ENGINE=paddleocr`.

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

`OCR_ENGINE` (`auto`), `PADDLE_LANG` (`en`), `PADDLE_ENABLE_MKLDNN` (`0`), `OCR_WARMUP` (off), `DEBUG_ENDPOINTS` (off). See `.env.example`.

## Licensing addendum

PaddleOCR / PaddlePaddle are Apache-2.0. PyMuPDF (used for rendering and the text layer) remains **AGPL-3.0**, see above.

## Page classifier (batch O3)

The `page.classifier` analyzer (`app/analyzers/page_classifier.py`, scoring in `app/classify.py`, vocabulary in
`app/classifier_lexicon.py`, thresholds in **`config/classifier.json`**) assigns every page exactly one `docType`:
`id_card`, `application_form`, `bank_statement`, `other`, `UNKNOWN` or `AMBIGUOUS`, plus `docTypeConfidence` in 0..1.
Pages are classified independently, so one PDF can contain different types. No LLM, no external call, no new dependency.

### Algorithm (deterministic and explainable)

1. **Keyword points** per class from OCR text: each matching lexicon rule adds its weight (title phrases such as
   "account statement" weigh most), repeats add a small bonus, and having many *different* keywords adds a combination bonus.
2. **Layout cues** add points only to a class that already has `cueGateRaw` keyword points (layout alone never creates a type):
   card-like aspect ratio and few lines (id_card); portrait pages and many `Label:` lines (application_form); portrait,
   date+amount **transaction rows**, a table header row and right-aligned amounts (bank_statement).
3. `score = 1 - exp(-points / scoreScale)` per class (0..1).
4. **Decision:** fewer than `minWords` words, or fewer than `minDistinctKeywords` distinct keywords in the best class, or best
   score `< minScore` -> `UNKNOWN`; best minus second `< ambiguityMargin` -> `AMBIGUOUS`; otherwise the best class.
5. **Confidence** = best score - `confidenceSeparationPenalty` x second score (clamped). For `UNKNOWN` it is the strength of the
   best, insufficient evidence and never exceeds `minScore`; a blank page is 0.0. Confidence is how closely the page resembles
   the label. It is unrelated to severity and says nothing about authenticity.

Blank pages and random text have no keywords, so they come out `UNKNOWN`, not forced into a type.

### Configuration (`config/classifier.json`, override the path with `CLASSIFIER_CONFIG`)

All values are required (no defaults in code) and validated on load. Main ones: `minScore` 0.45, `ambiguityMargin` 0.10,
`minWords` 6, `minDistinctKeywords` 2, `scoreScale` 6.0, `confidenceSeparationPenalty` 0.5, plus the layout-cue weights
(`transactionRowsMin` 3, `idCardAspectMin/Max` 1.3/1.8, ...). A missing or invalid file makes the analyzer report `ERROR`
(never a crash).

### Integration and provenance

* OCR is read through the shared context: OCR already cached by the `ocr` analyzer (or seeded recorded fixtures, see
  `app/ocr_fixtures.py`) is used first; otherwise `get_page_ocr` runs live OCR once. The classifier never OCRs a page twice.
* Results are stored in `ctx` (`classification_store(ctx)`, readable by later analyzers) and `/analyze` fills each
  `PageRecord.docType` / `docTypeConfidence` from them. Without the classifier, pages stay `UNKNOWN` with confidence 0.0.
* Status: `UNAVAILABLE` if no OCR is available for any page; `ERROR` if some pages failed (the others stay classified);
  otherwise `AVAILABLE` (even when every page is `UNKNOWN`).
* Provenance `config` echoes **every threshold** as `cfg.<name>` and per page `page<N>.docType`, `.confidence`, `.scores`,
  `.evidence` (the points that decided it) and `.note` (why `UNKNOWN` / `AMBIGUOUS`).

### Recorded OCR fixtures

`app/ocr_fixtures.py` reads `contracts/fixtures/ocr/<docId>-page-N.json` (override: `OCR_FIXTURES_DIR`), read-only.
`tests/test_classifier_fixtures.py` classifies them when the folder exists (D-01 -> id_card, D-02 -> application_form,
D-03 -> bank_statement) and is skipped otherwise.

### Limits

Heuristic and English-only: a page whose wording is not in the lexicon is `UNKNOWN`, and a page that mixes two document types
can be `AMBIGUOUS`. `other` is recognised only from its own vocabulary (invoice, letter, certificate, ...). The lexicon is
fictional-document vocabulary and has not been tuned on real scans. Analysts can correct any type in the UI.

## Field extractor (batch O4)

The `page.field_extractor` analyzer (`app/analyzers/field_extractor.py`; engine `app/extract.py`; schemas
`app/schemas/*.yaml`; normalizers and format-only validators `app/extract_normalize.py`) turns OCR words into contract
`ExtractedField` objects. New dependency: **PyYAML** (to read the schemas).

### What it extracts

| docType | fields |
| --- | --- |
| `id_card` | `full_name`, `dob`, `id_number`, `address_permanent` |
| `application_form` | `full_name`, `dob`, `address_permanent`, `address_current`, `requested_amount`, `signature` (region only) |
| `bank_statement` | `account_holder`, `account_number`, `opening_balance`, `closing_balance`, `total` |

The page type comes from the O3 classifier (`classification_store`); if it did not run, the same pure `classify_page` is applied
to the page's OCR. `other`, `UNKNOWN` and `AMBIGUOUS` pages are skipped, never forced into a type (status `NOT_APPLICABLE` when
no page qualifies). OCR is read from the context (cached OCR or seeded fixtures) and never computed twice.

### Schemas (`app/schemas/<docType>.yaml`, shared `_settings.yaml`)

Per field: `name`, `semanticType`, `normalizer`, `validators`, optional expected `region`, `continuationLines`, and ordered
`strategies` (the first that finds a value wins): `anchor` (label regexes + optional value `pattern`, `pick: first|last`),
`composite` (e.g. given names + surname) and `anchor_region` (signature box placed relative to the label). Anchors are regexes
matched case-insensitively and word-bounded; the longest match on a line wins. `semanticType` is kept in the context
(`extraction_store(ctx)`) for later batches; the contract's `ExtractedField` has no such field, so it is not emitted.
A missing or malformed schema or settings file makes the analyzer report `ERROR` with the reason (never a crash).
`FIELD_SCHEMAS_DIR` overrides the folder.

### Strategy

1. Find the label on an OCR line; the value is the text after it on that line (cut before the next `Label:`), else the value on
   the same row to its right, else on the line(s) below. Address-like fields append up to `continuationLines` unlabeled lines.
2. The pattern (`@date`, `@amount`, `@digits` or a regex) narrows the value. Text under a label that does not fit the pattern is
   still returned (raw), flagged `INVALID_FORMAT` and with lower extractionConfidence.
3. `rawValue` is the exact OCR text of the value (words joined by single spaces); it is never altered.
   `normalizedValue`: ISO `YYYY-MM-DD` dates, plain two-decimal amounts (`1,250.00` -> `1250.00`, no currency conversion),
   whitespace-collapsed text, digits-only account numbers; `null` when it cannot be normalized without guessing.
4. `region.bbox` is the union of the value's OCR word boxes (not the label), normalized `[x, y, w, h]` on the canonical page.
5. `ocrConfidence` = mean OCR confidence of the value words. `extractionConfidence` = the strategy's base confidence adjusted for
   placement, pattern misses, competing candidates and the expected region (`_settings.yaml`); it never uses OCR confidence.
6. `validationStatus` is a format check only (no lookups): `VALID`, `INVALID_FORMAT`, or `NOT_CHECKED` (no validator, or a
   well-formed but ambiguous date with no configured order).

Numeric dates with day and month both <= 12 (`12/08/2004`) are ambiguous. `dateOrder: DMY` in `_settings.yaml` follows the
shared contract example (`12/08/2004` -> `2004-08-12`) and every such normalization is noted; set `dateOrder: null` to leave
them un-normalized. Please confirm the convention with the samples owner.

Low-confidence and invalid fields are **never dropped**: they are returned with their confidences and status, and listed in
provenance (`page<N>.lowConfidence`, `.invalidFormat`, `.missing`). A field that is not found is not returned and not invented.
The signature is only a box (`rawValue` empty, `ocrConfidence` 0.0, `NOT_CHECKED`): nothing is claimed about the signature.

### Status and provenance

`AVAILABLE` (fields returned), `NOT_APPLICABLE` (no supported page), `UNAVAILABLE` (no OCR for any page), `ERROR` (an OCR page,
schema or settings problem; the registry then emits no fields from this analyzer, the other analyzers are unaffected).
Provenance `config` holds the settings and scoring weights, schema versions and per-page docType, fields, missing and flags.

### Accuracy report

`python tools/accuracy_report.py` (and `tests/test_extractor_accuracy.py`) run the real classifier and extractor on the recorded
OCR fixtures `contracts/fixtures/ocr/<docId>-page-N.json` and compare with **`samples/manifest.json`** (`SAMPLES_MANIFEST`,
`OCR_FIXTURES_DIR`), printing `Document type | Doc | Field | Expected | Extracted | Match`, per-field accuracy, unexpected extracted
fields and the comparisons that could not be performed. A missing expected field counts as a mismatch. If the manifest or the
fixtures are missing, it says so and compares nothing. The manifest shape accepted is documented in `app/extract_report.py` and is an
**assumption** until the samples owner confirms the real one. The test run also prints a clearly labelled SYNTHETIC table
(self-authored pages), which is not the acceptance comparison.

### Limits

Anchor/regex heuristics for English labels; no table parsing (balances come from labelled lines); names, addresses and IDs are
checked for format only; the `total` field takes the first amount on its row (lower confidence when several); the ID-number and
account-number formats are generic placeholders (made-up formats), to be tightened once the real sample formats are known;
text-layer words are not used (source is always `ocr`).

## Image forensics (batch O8)

Three analyzers share one page loader (`app/analyzers/image_forensics.py`, algorithms in `app/image_forensics.py`).
They report observations about pixels, never verdicts. Thresholds live in `config/image_forensics.json`
(override with `IMAGE_FORENSICS_CONFIG`) and every value is echoed into each signal's provenance as `cfg.<name>`.

| Analyzer | Signal type | What it observes |
|---|---|---|
| `image.copymove` | `COPY_MOVE` (one signal, two regions, labelled "matching region A/B") | Two places holding the same ink pixels. ORB self-matches vote for an offset; each candidate is then confirmed by comparing pixels. |
| `image.noise` | `NOISE_INCONSISTENCY` | Paper-only blocks much quieter than the rest of the page (typical of a flat fill or erased area). |
| `image.ela` | `ELA_ANOMALY` (confidence capped at 0.4) | JPEG re-save residual that is unusual for the edge detail of a block. JPEG, unrotated originals only. |

Applies to image originals and to scanned PDF pages (a page with no text-layer words). A digital PDF page is
`NOT_APPLICABLE`. A test that cannot run (a clean digital image has no scan noise to measure, a PNG has no JPEG
history) is reported `NOT_APPLICABLE` with the reason; it is never reported as a clean result.

Copy-move ignores: tiny matches, repeated words inside a line that otherwise differs (a merchant name on two
rows), shifted copies that overlap themselves, and regular patterns (three or more equal copies, such as ruled
lines or identical rows). It reports a duplicated text line only when nearly all of the line's ink is duplicated
(`cmMinCoverage`, 0.95).

### Measured on the D2 samples (25 images: 4 image tampers, 21 genuine or recompressed)

| Sample | Result |
|---|---|
| T-04 pasted statement row | found (`image.copymove`, both ends of the copy reported) |
| T-05 replaced signature | found (`image.noise`, quieter area over the signature box, IoU about 0.6) |
| T-03 retyped digits | **not detected** |
| T-06 swapped photo (PNG) | **not detected** |
| All genuine, scan, "hard" and recompressed controls | no signal from any of the three |

Known limits, stated plainly:
- **ELA found nothing on any D2 edit.** Every sample is saved once after editing, so edited and unedited areas share
  one compression history; the threshold is set high (`elaZThreshold` 25) because large heading text on clean
  statements scores up to about 15. Treat ELA as supporting evidence only.
- A pasted piece that is only part of a line (a single amount cell) is not found by `image.copymove`: it needs
  nearly the whole line duplicated. Small pastes are ignored by design.
- The noise test needs a scanned or photographed page; on a clean digital image it does not run.
- Thresholds were chosen on these 25 synthetic samples; they are not validated on real-world documents.
- Analysis runs on the canonical render, downscaled to `maxWorkingSidePx` (2600) if larger; boxes are fractions.

Tests: `tests/test_image_forensics.py` (synthetic pages) and `tests/test_image_forensics_samples.py` (the samples
above; the two undetected edits are `xfail`, deliberately not tuned to pass).

## D1 accuracy evaluation (real pipeline)

`tools/d1_accuracy_report.py` runs the **production pipeline** (`render_document` -> OCR -> `page.classifier` ->
`page.field_extractor`, the same steps as `POST /analyze`) on the real D1 source files listed in `../samples/manifest.json` and
compares the extracted fields with the manifest's expected values. It is separate from the C1 report above: the C1 OCR fixtures
(D-01, D-02, D-03) are not D1 documents and are never used here (and `tools/accuracy_report.py` now refuses a D1 manifest).

```powershell
cd ocr-service
python tools/d1_accuracy_report.py                       # default manifest: ..\samples\manifest.json
python tools/d1_accuracy_report.py --manifest ..\samples\manifest.json --dpi 150 --timeout 120 --json d1-report.json
```

* Manifest: `{"files": [{"doc_id", "doc_type", "variant", "path", "page", "fields": [{"name", "value", "bbox"}]}]}`. `path` is resolved
  against the manifest's folder, then its parent; `..` and absolute paths are rejected. The manifest and sample files are only read
  (each file is copied into a temporary evidence folder).
* Digital and scan entries share a `doc_id`, so each file gets a unique runtime id (`<doc_id>__<variant>`); nothing is overwritten.
  Repeated field names (e.g. several `txn_amount`) are kept and matched **one-to-one**: exact values are paired first, left-overs
  are paired in reading order as mismatches, and an extracted field is never used twice. Matching is by field name **and page**.
* A value matches when the expected string equals the extracted `normalizedValue` or `rawValue` exactly (surrounding spaces
  ignored, no case folding); the report says which one matched.
* Result per expected field: `MATCH`, `MISMATCH` (a value was found but differs), `MISSING` (supported field, nothing extracted),
  `NOT EVALUATED` (the O4 schema has no such field; it is **not** an extraction failure and fields are **never renamed** to fit),
  `FAILED` (the sample's render/OCR/analyzers failed: `ERROR` / `UNAVAILABLE`, nothing compared). Extracted fields nobody
  expected are `UNEXPECTED` and listed separately.
* Bounding-box IoU (expected `[x, y, w, h]` vs the extracted `region.bbox`) is informational and never part of value accuracy.
  A manifest box that is not a normalized `[x, y, w, h]` is flagged and skipped.
* The summary is given for digital, scan and overall with the denominators: samples ok, expected, evaluated (= matched + mismatched +
  missing), matched, mismatched, missing, not evaluated, failed, unexpected, "accuracy of evaluated" and "accuracy of all expected".
  Whenever fields are not evaluated or failed, the report prints `COVERAGE IS INCOMPLETE`.
* Exit code: 0 every expected field matched; 1 some mismatch / missing / not-evaluated field; 2 manifest missing or unusable (nothing
  run); 3 at least one sample failed. `OCR_ENGINE` / `TESSERACT_CMD` apply as for the service; the first PaddleOCR use is slow, so
  raise `--timeout` if a sample reports a timeout.

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
