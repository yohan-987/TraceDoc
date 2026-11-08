"""Real OCR (installed Tesseract) -> classifier, on generated fictional page images. Skipped without Tesseract."""
import pytest

from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.classify import classification_store
from app.context import AnalysisContext
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from PIL import Image
import io

from tests import classifier_pages as P
from tests.ocr_helpers import tesseract_available

pytestmark = pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed")

CASES = [("id", "id_card", P.ID_CARD_LINES, (1011, 638)), ("form", "application_form", P.FORM_LINES, (1240, 1754)),
         ("statement", "bank_statement", P.STATEMENT_LINES, (1240, 1754)),
         ("invoice", "other", P.INVOICE_LINES, (1240, 1754)), ("letter", "other", P.LETTER_LINES, (1240, 1754))]


def _classify(settings, put_original, doc_id, png):
    put_original(doc_id, "original.png", png)
    ctx = AnalysisContext(render_document(doc_id, settings), settings)
    registry = AnalyzerRegistry()
    registry.register(TesseractOcrAnalyzer)
    registry.register(PageClassifierAnalyzer)
    outcome = run_analyzers(ctx, registry, 120.0)
    return outcome, classification_store(ctx)[1]


@pytest.mark.parametrize("name,expected,lines,size", CASES, ids=[c[0] for c in CASES])
def test_real_ocr_pages_are_classified(settings, put_original, name, expected, lines, size):
    outcome, result = _classify(settings, put_original, f"RC-{name}", P.render_png(lines, *size))
    assert outcome.statuses == {"ocr": "AVAILABLE", "page.classifier": "AVAILABLE"}
    assert result.docType == expected and 0.0 <= result.confidence <= 1.0


def test_real_blank_page_is_unknown(settings, put_original):
    buf = io.BytesIO()
    Image.new("RGB", (1240, 1754), "white").save(buf, "PNG")
    outcome, result = _classify(settings, put_original, "RC-blank", buf.getvalue())
    assert outcome.statuses["page.classifier"] == "AVAILABLE"
    assert result.docType == "UNKNOWN" and result.confidence == 0.0


def test_real_random_text_page_is_unknown(settings, put_original):
    lines = [" ".join(line.split()) for line in
             (" ".join(w.text for w in P.random_text(seed=5).words[i:i + 8]) for i in range(0, 64, 8))]
    _, result = _classify(settings, put_original, "RC-random", P.render_png(lines, 1240, 1754))
    assert result.docType == "UNKNOWN"
