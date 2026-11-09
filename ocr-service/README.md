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
| `application_form` | `full_name`, `id_number`, `dob`, `address_permanent`, `address_current`, `requested_amount`, `application_date`, `signature` (region only) |
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

1. Find the label on an OCR line. The value is then looked for in three places: after the label on the same line (cut before
   the next label), to its right on the same row, and **below** it (label above value, inside the label's column). The nearest
   candidate that validates for the field type wins; if none validates, the nearest non-label text is returned flagged
   `INVALID_FORMAT` (never dropped). Address-like fields append up to `continuationLines` unlabeled lines. See "Label-to-value
   association" below.
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

### Label-to-value association (O4 fix)

The first version took the next token or line after a label even when that text was itself another label or a format hint, so a
tampered case with a real DOB conflict (S5a) produced no findings: the id card gave `dob = 'ISSUED'`, `id_number = 'EXPIRES'`, the
form `full_name = 'ID number'`, `dob = '(YYYY-MM-DD)'`, `requested_amount = 'Term'`, the statement `account_holder = 'Account summary'`.

What changed (`app/extract.py`, schemas, `_settings.yaml`):

* **Label vocabulary.** A schema may list `labels:` (printed labels and headings that are not extracted fields: `ISSUED`, `EXPIRES`,
  `Term`, `Guarantor ID number`, `Account summary` ...); `_settings.yaml` has a shared `labelVocabulary`. Together with every anchor
  of the schema they form the vocabulary. Text made only of vocabulary labels, or only a format hint (`(YYYY-MM-DD)`, `(optional)`;
  parenthesised text without letters, such as a negative amount, is not a hint), is **never returned as a value**. Vocabulary matching
  tolerates the usual OCR look-alikes (`I`/`1`/`l`/`|`, `O`/`0`), so `1SSUED` and `1D NUMBER` are still recognised as labels. That tolerance is
  used to recognise labels only, never to find a field's own anchor.
* **Geometry, not line order.** Below a label the value is taken from the OCR **word boxes**: the nearest row of words inside the
  label's column (from `geometry.columnLeftTolerance` left of the label to the start of the next label on its row, or overlapping a
  right-aligned label). Words further left (a photo's OCR noise, another column) are excluded; a row that starts with another
  column's label is skipped, a row of pure labels ends the search. Rows are formed by chaining word centres, so a slightly rotated
  scan stays one row. Among labelled occurrences the top-most, left-most wins (reading order by geometry).
* **Validated candidates first.** Candidate values must validate for the field type (date parses, ID format, amount parses, name looks
  like a name); a validating candidate beats a nearer one that does not. A value on the label's own line counts as adjacent;
  right-of-label and below-label candidates compete on box distance.
* **Another person's label is not the anchor.** An anchor match lying inside a longer known label (`full name` inside `Guarantor full
  name`) is ignored, so the guarantor's name/ID/DOB are never returned as the applicant's (they are reported missing instead).
* **ID and account formats.** The generic ID format now requires at least one digit. Account numbers keep a printed letter prefix
  (`NSB-5404-470671`: new `account` normalizer and `@account` pattern; purely numeric numbers are still digits-only).
* **Bare labels.** `^name` / `^address` anchors also match a label alone on its line (`NAME`, no colon).
* **New form fields:** `id_number` and `application_date` (`application_form`).
* **D1 harness:** `app/d1_eval.py` maps the manifest's `loan_application` to the `application_form` schema and
  `date_of_birth`/`permanent_address`/`current_address`/`requested_loan_amount` to the schema names. Before this, loan forms and every
  date of birth were reported NOT EVALUATED, so these fields were never measured. Only names are mapped, never values.

Measured with `python tools/d1_accuracy_report.py` (real Tesseract pipeline, 24 D1 files: 3 document types x 2 people x original /
recompressed / scan / scan_hard). **Both columns use the same extended harness**; "before" is the previous extractor (commit
`3a06ce4`). With the harness as it was, the previous extractor scored 18/48 (37.5%) on the 48 fields it could evaluate.

| docType | field | before | after |
| --- | --- | --- | --- |
| bank_statement | account_holder | 2/8 (25%) | 6/8 (75%) |
| bank_statement | account_number | 0/8 (0%) | 7/8 (88%) |
| bank_statement | closing_balance | 8/8 (100%) | 8/8 (100%) |
| bank_statement | opening_balance | 8/8 (100%) | 8/8 (100%) |
| id_card | date_of_birth | 0/8 (0%) | 8/8 (100%) |
| id_card | full_name | 0/8 (0%) | 0/8 (0%) |
| id_card | id_number | 0/8 (0%) | 6/8 (75%) |
| id_card | permanent_address | 0/8 (0%) | 0/8 (0%) |
| loan_application | application_date | not evaluated | 8/8 (100%) |
| loan_application | current_address | 6/8 (75%) | 6/8 (75%) |
| loan_application | date_of_birth | 0/8 (0%) | 8/8 (100%) |
| loan_application | full_name | 0/8 (0%) | 5/8 (62%) |
| loan_application | id_number | not evaluated | 6/8 (75%) |
| loan_application | permanent_address | 8/8 (100%) | 8/8 (100%) |
| loan_application | requested_loan_amount | 0/8 (0%) | 7/8 (88%) |
| **all evaluated** | | **32/104 (30.8%)** | **91/120 (75.8%)** |

Like for like (excluding the two fields added to the form schema): 32/104 (30.8%) before, 77/104 (74.0%) after. By variant after the
fix: original 25/30, recompressed 24/30, scan 24/30, scan_hard 18/30 (evaluated fields).

Live pipeline on `samples/dev/S5a_tampered_with_inconsistency` (Tesseract): id card `full_name` PRIYANKA WHITLOCK, `dob`
1980-09-18, `id_number` SMP-1701-9929-AK; loan form `full_name` Priyanka Whitlock, `dob` 1983-09-18 (the DOB conflict is now visible),
`id_number` SMP-1701-9929-AK, `requested_amount` 20500.00, `application_date` 2026-03-14; statement `account_holder` Priyanka Whitlock,
`account_number` NSB-4596-913745.

Tests: `tests/test_extract_association.py` (an observed failure each, a "label is never a value" test, guarantor, garbled labels,
skew, `INVALID_FORMAT` kept). Full suite: 585 passed, 4 skipped, 4 xfailed.

#### Remaining misses (not fixed, not hidden)

* **ID card `full_name` and `permanent_address` (0/8 each) are not association errors.** The card prints them in capitals
  (`BRADLEY EVANS`, `06028 BARRY HARBORS PORT JOSEPH, GU 44394`) while the manifest holds `Bradley Evans` / `06028 Barry Harbors, Port
  Joseph, ...`; the report compares exactly. `rawValue` is the printed text and is never altered; no case or comma is guessed into
  `normalizedValue`. Downstream comparison of names across documents should therefore be case-insensitive.
* **OCR errors in the value or in the field's own label:** `Marisol lvanova` (I read as l) on 2 loan forms; a stray `.` inside a name
  (`Marisol . Ivanova`); `id_number` MISSING on 4 scan/scan_hard files (in the cases inspected its own label is misread, and a field is not guessed from a label that is not recognised).
* **scan_hard** (rotated, noisy): `account_number` cut to `NSB-51`; `account_holder` merges neighbouring rows
  (`Marisol Address Ivanova ...`: chaining word centres follows the rotation but can join rows when the skew exceeds the line pitch);
  `requested_amount` MISSING on one form; an address continuation runs into the next section heading (`... 2. Loan request`) on two forms.
* `total` (bank statement) is no longer returned for a row such as `Totals / closing balance 1,667.36 5,081.26 5,517.57`: the
  value would start at another label. Table totals belong to `layout.tables`.
* Guarantor fields are not extracted (no schema field); they are only prevented from being read as the applicant's.
* The D1 report still evaluates only fields that have a schema field (744 of 864 expected fields are NOT EVALUATED: transactions,
  issue/expiry dates, guarantor, ...). No D1 accuracy claim should be made beyond the 120 evaluated fields.
* No sample was special-cased and no threshold tuned to pass; `columnLeftTolerance` (0.02, about one text-line height) was fixed
  from the layout before measuring.

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
account-number formats are still generic (ID: 5-25 letters/digits/hyphens with a digit; account: optional letter prefix plus digit groups);
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

## Layout and tables (batch O5)

Plugin `layout.tables` (`app/analyzers/layout_tables.py`, pure geometry in `app/layout.py`, thresholds and the header
vocabulary in `config/layout.json`, override with `LAYOUT_CONFIG`; every value is echoed into provenance as `cfg.<name>`).
It reads the OCR already in the shared context and, for pages the classifier calls `bank_statement`, reads the
transaction table from word geometry:

1. words are grouped into rows (after compensating a small page tilt, up to `skewMaxDeg`, estimated from the words);
2. a header row naming at least `minHeaderRoles` of Date / Description / Debit / Credit / Balance defines the columns;
3. rows that start with a date are transactions; amount words go to the nearest amount column by right edge, centre or
   left edge (right-aligned numbers share a right edge); a missing cell stays missing and never shifts a neighbour;
4. the first later row starting with "Total(s)" supplies the stated totals and ends the table;
5. a page without its own header inherits the previous page's columns (statements that run over several pages).

Emitted fields (ids `TBL-001`..., unique per document and continuing across pages): `txn_amount` for each CREDIT row,
`debit_amount` for each DEBIT row, `total_credits` and `total_debits` from the Totals row. The backend sums
`txn_amount` against `total_credits` (R-STMT-TOTAL-ARITH) and `debit_amount` against `total_debits`
(R-STMT-DEBIT-ARITH). `rawValue` is the printed text, `normalizedValue` a plain two-decimal string; an amount that
cannot be read (for example `2.400,00`) is still returned as `INVALID_FORMAT` so a downstream sum is never silently short.
Provenance lists per page the columns used and where they came from, the row count, tilt, rows with problems,
`unreadableAmounts`, `totalsRows` and `balanceChainBreaks` (rows whose printed balance is not the previous balance plus
credit minus debit; a count of observations, not a conclusion). Status: `NOT_APPLICABLE` when no page is a statement,
`AVAILABLE` otherwise (zero fields plus `tablesFound=0` when no table is recognised), `UNAVAILABLE` when OCR is not available.

### Measured on the D1 statement samples (live Tesseract, 200 dpi render, `tests/test_layout_samples.py`)

| Variant | Result |
|---|---|
| digital PDF (both statements) | all credit rows, debit rows and both totals exact |
| scan (both) | exact (needs the tilt compensation) |
| recompressed (both) | exact |
| scan_hard (both) | **not exact, recorded as xfail**: Tesseract misreads digits (1 read as 4, 11.04 as 41.04) and loses one row date; the layout is read correctly and `balanceChainBreaks` flags the affected rows |

Limits: one table layout only (separate Debit and Credit columns; a single signed Amount column or a Dr/Cr suffix is not
read); wrapped description lines are ignored; a statement whose later pages have no header and different column positions
would be mis-assigned (provenance says "previous page"); more than one Totals row (per-page subtotals) emits all of them.
Column assignment cannot repair an OCR misread, only report what was printed.

## Text layer vs render (batch O6)

Analyzer `pdf.textlayer` (`app/analyzers/pdf_text_layer_mismatch.py`, comparison in `app/textlayer_check.py`).
It compares the words a digital PDF carries in its text layer with what OCR reads on the rendered page and reports
**where the two disagree**. It reports an observation (`TEXT_LAYER_MISMATCH`), never who changed what or why. The id is
`pdf.textlayer` because that is what `backend/config/engine.json`, the frontend and `contracts/fixtures/analyze/D-03.json`
use; the older `pdf.text_layer_words` analyzer (O2b, the layer itself) is a different thing and stays.

| `measurements.kind` | Meaning | How it is decided |
|---|---|---|
| `layer_only` | Words are in the layer and nothing is visible there (white on white, invisible render mode, covered by a patch, too small to read). Confidence 0.70 up to 0.85 with more words. | The **pixels** under the word are blank (outside the words OCR did read), or the word is under 5 px high. OCR alone is not enough: a word OCR merely failed to read has ink under it and is only counted (`layerWordsWithInkButNoOcr`). |
| `differs` | The layer says one thing, the page visibly shows another at the same place (stale or overlaid text). Two regions: "what the text layer says" / "what the page shows". Confidence 0.75. | The OCR word must really sit on the layer word (a share of each lies over the other, so an OCR box inflated by a highlight does not count), and a second reading from a re-render at twice the resolution must still disagree. Small text is often misread (1 for 4); if the sharper reading agrees with the layer nothing is reported (`differsExplainedByRereading`). Without Tesseract the candidate is kept and counted in `differsUnverified`. |
| `render_only` | Visible text the layer lacks. Confidence 0.35. **Off by default** (`flagRenderOnly`). | On the 44 clean sample PDFs it produced one false signal (S3b, drawn text), so it is opt-in. |

Measurements follow the recorded fixture (`renderedText`, `textLayerText`, `differingChars`) plus `kind`, `reason`,
`rereadText`, `pageAgreement`, `pageLayerWords`, `pageOcrWords`, `scope`. Signal ids are `<docId>-tlmm-p<page>-<nn>`.

Status: `NOT_APPLICABLE` for image originals and for PDFs with no text layer (a scan; nothing to compare is not "clean").
`ERROR` when a page cannot be compared (OCR failed, more than `maxWordsPerPage` words, bad config), naming the pages.
`UNAVAILABLE` when OCR read too little of every page to judge (rotated 180/270, shaded backgrounds) or no engine exists.
If OCR read plenty but barely agrees with the layer, one page-level signal (`scope = page`, confidence 0.35) is added.

### Thresholds (`config/text_layer_check.json`, override with `TEXT_LAYER_CHECK_CONFIG`)

Every value is echoed into each signal's provenance as `cfg.<name>`; NaN/Infinity and nonsensical values are refused at load.

| Name | Default | Meaning |
|---|---|---|
| `textSimilarityMin` / `numericSimilarityMin` | 0.6 / 0.9 | Two readings agree at or above this similarity. Digits are stricter: one wrong digit is a different value. Common OCR confusions (O/0, l/1/I, S/5, B/8) are folded first. |
| `minWordChars`, `minGroupChars` | 2, 3 | Words, and mismatching groups, with fewer letters/digits are ignored (tiny differences). |
| `minOcrConfidence` | 0.5 | OCR words below this are not trusted as visible text. |
| `differsMinOverlap`, `differsMinOcrShare` | 0.5, 0.3 | What "the same place" means for `differs`. |
| `inkDelta`, `inkMinFraction`, `inkPadPx` | 40, 0.01, 1 | A pixel is ink if it differs from the local background by more than `inkDelta`; an area under `inkMinFraction` ink is blank. At least 70 % of the word's box must lie outside words OCR read, else it is not judged. |
| `minBoxHeightPx`, `tinyHeightPx`, `edgeMarginFrac` | 2, 5, 0.008 | Degenerate boxes are ignored; an unread word under `tinyHeightPx` is reported as too small to read; words touching the page edge (clipped glyphs) are not judged. |
| `groupGapFactor` | 1.5 | Neighbouring mismatches (beside, or stacked in a column) closer than this many word heights form one signal. |
| `minPageAgreement`, `minOcrCoverage`, `minWordsForAgreement` | 0.25, 0.5, 6 | Page-level handling: OCR that read fewer than half as many words as the layer holds means "not comparable", not a finding. |
| `rereadScale`, `rereadPadFrac`, `rereadTimeoutS`, `maxRereadsPerPage` | 2.0, 0.3, 10, 10 | The second reading of `differs` candidates (`rereadScale` 0 turns it off). |
| `maxWordsPerPage`, `maxSignalsPerPage` | 20000, 25 | Work and output bounds (overflow is counted in `signalsDropped`). |
| `hiddenConfidence`, `hiddenConfidencePerWord`, `hiddenConfidenceMax`, `differsConfidence`, `renderOnlyConfidence`, `pageLevelConfidence` | 0.70, 0.04, 0.85, 0.75, 0.35, 0.35 | Signal confidences. |

### Measured (real Tesseract)

| Check | Result |
|---|---|
| T-08 white text, T-09 invisible text (`samples/tampered`) | one signal each, `layer_only`, the hidden text read back exactly (`USD 56,500.00`, `1969-12-31`), IoU with the ground-truth box 0.79 and 0.79 |
| The other 44 sample PDFs (genuine, controls, other tampers, dev and test scenarios) | no signal, at 150 dpi and at the 100 dpi of the test fixture |
| 200 generated genuine pages (random fonts, sizes, colours, shaded and dark bands, rotation) at 100, 150 and 200 dpi | no signal |
| The same pages with a hidden item added: white text, invisible mode, covered by a patch, 1.2 pt font, near-white text, text under an image patch | all 30 found, once each |
| Searchable scan (picture plus matching invisible layer) / the same plus one extra invisible line | no signal / the extra line, once |

Tests: `tests/test_text_layer_check.py` (algorithm), `tests/test_textlayer_check_config.py`,
`tests/test_text_layer_mismatch_analyzer.py` (simulated OCR, no engine needed) and `tests/test_text_layer_mismatch_samples.py`
(real OCR over every sample PDF; skipped without Tesseract).

### Limits, stated plainly

- **Text under something drawn is not found.** If hidden text lies under a picture, a rule or other visible ink and OCR does
  not read that ink as the same words, the area is not blank and the word is only counted in `layerWordsWithInkButNoOcr`.
- **Text placed outside the page** is dropped by the PDF text extraction before this check sees it.
- **A hidden value that happens to occur inside a visible one at the matching position** passes as a merged reading.
- **OCR limits become coverage limits.** Rotated 180/270 pages and some shaded pages are not read, so they are reported
  `UNAVAILABLE`, not clean. Small text at low `RENDER_DPI` is read worse; the second reading needs Tesseract.
- Thresholds were set on the D1/D2 samples plus generated pages, all synthetic; they are not validated on real-world PDFs.
- Mismatch means "the two readings disagree here". Legitimate causes exist (OCR layers added to scans, watermarks, fonts OCR cannot read).

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
