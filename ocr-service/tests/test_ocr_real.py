"""REAL OCR integration tests: they call the installed Tesseract (skipped if it is not available).

Assertions are deliberately loose (words found, boxes sane, confidences in range) because exact OCR
output and confidence vary by Tesseract version and platform.
"""
import pytest

from app import ocr, ocr_core
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.context import AnalysisContext
from app.models import AnalyzeResponse, OcrPage
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.ocr_helpers import tesseract_available, text_pdf_bytes, text_png_bytes

pytestmark = pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed")


@pytest.fixture(autouse=True)
def _restore_tesseract_cmd(monkeypatch):
    """ocr_image_file sets pytesseract's global command; put it back after each test."""
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd",
                        ocr_core.pytesseract.pytesseract.tesseract_cmd)

KNOWN = ["FICTIONAL", "INVOICE", "12345", "Jane", "Example"]
LINES = ["FICTIONAL INVOICE 12345", "Name: Jane Example"]


def _ocr(settings, put_original, doc_id, name, data):
    put_original(doc_id, name, data)
    ctx = AnalysisContext(render_document(doc_id, settings), settings)
    registry = AnalyzerRegistry()
    registry.register(TesseractOcrAnalyzer)
    outcome = run_analyzers(ctx, registry, 60.0)
    return ctx, outcome


def _found(page, expected):
    texts = " ".join(w.text for w in page.words).lower()
    return [e for e in expected if e.lower() in texts]


def test_english_text_from_png(settings, put_original):
    ctx, outcome = _ocr(settings, put_original, "R-PNG", "original.png", text_png_bytes(LINES))
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    page = ocr.get_page_ocr(ctx, 1)
    OcrPage.model_validate(page.model_dump())  # shared contract
    assert page.page == 1 and page.engine == "tesseract"
    assert (page.widthPx, page.heightPx) == (ctx.pages[0].width_px, ctx.pages[0].height_px)
    assert len(_found(page, KNOWN)) >= 4  # at least 80% of the known strings


def test_boxes_lines_and_confidence(settings, put_original):
    ctx, _ = _ocr(settings, put_original, "R-BOX", "original.png", text_png_bytes(LINES))
    page = ocr.get_page_ocr(ctx, 1)
    assert page.words and len(page.lines) >= 2
    for w in page.words:
        x, y, bw, bh = w.bbox
        assert 0 <= x < 1 and 0 <= y < 1 and bw > 0 and bh > 0
        assert x + bw <= 1 + 1e-9 and y + bh <= 1 + 1e-9
        assert 0 <= w.ocrConfidence <= 1 and w.text.strip()
        assert page.lines[w.lineIndex].wordIndices.count(page.words.index(w)) == 1
    for i, line in enumerate(page.lines):
        assert 0 <= line.ocrConfidence <= 1
        assert all(page.words[j].lineIndex == i for j in line.wordIndices)
    # the two drawn text lines sit at different heights, top line first
    assert page.lines[0].bbox[1] < page.lines[1].bbox[1]
    # words within a line run left to right
    first = [page.words[j].bbox[0] for j in page.lines[0].wordIndices]
    assert first == sorted(first)
    assert sum(w.ocrConfidence for w in page.words) / len(page.words) > 0.5


def test_english_text_from_pdf(settings, put_original):
    from app.config import Settings
    cfg = Settings(**{**settings.__dict__, "render_dpi": 150})
    ctx, outcome = _ocr(cfg, put_original, "R-PDF", "original.pdf", text_pdf_bytes([LINES]))
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    assert len(_found(ocr.get_page_ocr(ctx, 1), KNOWN)) >= 4


def test_multiple_pdf_pages(settings, put_original):
    from app.config import Settings
    cfg = Settings(**{**settings.__dict__, "render_dpi": 150})
    pdf = text_pdf_bytes([["ALPHA ONE"], ["BRAVO TWO"], ["CHARLIE THREE"]])
    ctx, outcome = _ocr(cfg, put_original, "R-MULTI", "original.pdf", pdf)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    assert outcome.provenance[0].config["pages"] == 3
    for n, word in [(1, "alpha"), (2, "bravo"), (3, "charlie")]:
        page = ocr.get_page_ocr(ctx, n)
        assert page.page == n
        assert word in " ".join(w.text for w in page.words).lower()


def test_blank_page_real(settings, put_original):
    ctx, outcome = _ocr(settings, put_original, "R-BLANK", "original.png", text_png_bytes([]))
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    page = ocr.get_page_ocr(ctx, 1)
    assert page.words == [] and page.lines == []


def test_analyze_endpoint_with_real_ocr(make_client, put_original):
    put_original("R-API", "original.png", text_png_bytes(LINES))
    registry = AnalyzerRegistry()
    registry.register(TesseractOcrAnalyzer)
    body = make_client(registry).post("/analyze", json={"docId": "R-API"}).json()
    AnalyzeResponse.model_validate(body)
    assert body["analyzerStatuses"] == {"ocr": "AVAILABLE"}
    assert body["provenance"][0]["config"]["words"] >= 4
