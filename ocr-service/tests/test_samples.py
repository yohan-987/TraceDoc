"""Generated sample documents (ID card, form, statement): at least 80% of the known strings must be found.

Real-engine tests are skipped when the engine is not installed. The text-layer tests need no OCR engine.
"""
import importlib.util

import pytest

from app import ocr, textlayer
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.config import Settings
from app.context import AnalysisContext
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests import samples
from tests.ocr_helpers import tesseract_available, text_pdf_bytes

THRESHOLD = 0.8
SAMPLES = [("id", samples.id_card), ("form", samples.form), ("statement", samples.statement)]


def _ocr_rate(settings, put_original, doc, data, known):
    put_original(doc, "original.png", data)
    ctx = AnalysisContext(render_document(doc, settings), settings)
    registry = AnalyzerRegistry()
    registry.register(TesseractOcrAnalyzer)
    outcome = run_analyzers(ctx, registry, 120.0)
    assert outcome.statuses == {"ocr": "AVAILABLE"}, outcome.provenance[0].config
    page = ocr.get_page_ocr(ctx, 1)
    return samples.detection_rate([w.text for w in page.words], known), page


@pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed")
@pytest.mark.parametrize("kind,make", SAMPLES)
def test_tesseract_detects_known_strings(settings, put_original, kind, make):
    data, known = make()
    rate, page = _ocr_rate(settings, put_original, f"S-{kind}", data, known)
    assert page.engine == "tesseract"
    assert rate >= THRESHOLD, f"{kind}: detected {rate:.0%}"


@pytest.mark.skipif(importlib.util.find_spec("paddleocr") is None, reason="PaddleOCR is not installed")
@pytest.mark.parametrize("kind,make", SAMPLES)
def test_paddleocr_detects_known_strings(settings, put_original, monkeypatch, kind, make):
    monkeypatch.setenv("OCR_ENGINE", "paddleocr")  # no fallback: this must be PaddleOCR's own result
    data, known = make()
    rate, page = _ocr_rate(settings, put_original, f"P-{kind}", data, known)
    assert page.engine == "paddleocr"
    assert rate >= THRESHOLD, f"{kind}: detected {rate:.0%}"


@pytest.mark.parametrize("kind", ["id", "form", "statement"])
def test_pdf_text_layer_detects_known_strings(settings, put_original, kind):
    lines = samples.text_lines(kind)
    put_original(f"T-{kind}", "original.pdf", text_pdf_bytes([lines]))
    ctx = AnalysisContext(render_document(f"T-{kind}", settings), settings)
    layer = textlayer.get_text_layer_page(ctx, 1)
    assert layer.source == "pdf_text_layer"
    known = [part.split(": ")[-1] for part in lines]  # field values, e.g. "AB1234567"
    assert samples.detection_rate([w.text for w in layer.page.words], known) >= THRESHOLD
