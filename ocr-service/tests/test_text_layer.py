"""Digital-PDF text-layer extraction (PyMuPDF, source "pdf_text_layer")."""
import pymupdf
import pytest

from app import ocr, ocr_core, textlayer
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.pdf_text_layer import PdfTextLayerWordsAnalyzer
from app.config import Settings
from app.context import AnalysisContext
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.conftest import make_png_bytes
from tests.ocr_helpers import fake_tsv, tesseract_available, text_pdf_bytes

LINES = ["FICTIONAL INVOICE 12345", "Name: Jane Example"]


def _ctx(settings, put_original, doc, data, name="original.pdf"):
    put_original(doc, name, data)
    return AnalysisContext(render_document(doc, settings), settings)


def _run(ctx, *analyzers):
    registry = AnalyzerRegistry()
    for a in analyzers:
        registry.register(a)
    return run_analyzers(ctx, registry, 30.0)


def test_words_to_page_dict_normalizes_and_groups():
    raw = [(60, 100, 160, 130, "Hello", 0, 0, 0), (170, 100, 260, 130, "World", 0, 0, 1),
           (60, 200, 140, 230, "Next", 0, 1, 0), (10, 10, 20, 20, "  ", 0, 2, 0)]
    page = textlayer.words_to_page_dict(raw, 1190, 1684, 2.0, 2.0)  # A4 pt -> 2 px per point
    assert [w["text"] for w in page["words"]] == ["Hello", "World", "Next"]  # blank token skipped
    assert page["words"][0]["bbox"] == pytest.approx((120 / 1190, 200 / 1684, 200 / 1190, 60 / 1684))
    assert [w["lineIndex"] for w in page["words"]] == [0, 0, 1]
    assert page["lines"][0]["text"] == "Hello World" and page["lines"][0]["wordIndices"] == [0, 1]
    assert all(w["ocrConfidence"] == 1.0 for w in page["words"])


def test_words_outside_the_page_are_clipped_or_dropped():
    raw = [(-30, 10, 40, 30, "left", 0, 0, 0), (900, 10, 950, 30, "gone", 0, 0, 1)]
    page = textlayer.words_to_page_dict(raw, 595, 842, 1.0, 1.0)
    assert [w["text"] for w in page["words"]] == ["left"]
    x, y, w, h = page["words"][0]["bbox"]
    assert x == 0 and x + w <= 1 and y + h <= 1


def test_digital_pdf_text_layer(settings, put_original):
    ctx = _ctx(settings, put_original, "TL-1", text_pdf_bytes([LINES]))
    layer = textlayer.get_text_layer_page(ctx, 1)
    assert layer.source == "pdf_text_layer" == textlayer.SOURCE
    assert layer.page.engine == textlayer.ENGINE and layer.page.page == 1
    assert (layer.page.widthPx, layer.page.heightPx) == (ctx.pages[0].width_px, ctx.pages[0].height_px)
    texts = [w.text for w in layer.page.words]
    assert {"FICTIONAL", "INVOICE", "12345", "Jane", "Example"} <= set(texts)
    for w in layer.page.words:
        x, y, bw, bh = w.bbox
        assert 0 <= x < 1 and 0 <= y < 1 and bw > 0 and bh > 0
        assert x + bw <= 1 + 1e-9 and y + bh <= 1 + 1e-9
        assert w.ocrConfidence == 1.0
    first = layer.page.words[0]  # text was inserted at (60, 120) pt on a 595 x 842 pt page
    assert 0.08 < first.bbox[0] < 0.13 and 0.08 < first.bbox[1] < 0.16
    assert len(layer.page.lines) == 2 and layer.page.lines[0].text.startswith("FICTIONAL")


def test_multi_page_pdf_text_layer(settings, put_original):
    ctx = _ctx(settings, put_original, "TL-M", text_pdf_bytes([["ALPHA ONE"], ["BRAVO TWO"], ["CHARLIE THREE"]]))
    for n, word in [(1, "ALPHA"), (2, "BRAVO"), (3, "CHARLIE")]:
        assert word in [w.text for w in textlayer.get_text_layer_page(ctx, n).page.words]


def test_rotated_pdf_page_boxes_follow_the_rotated_render(settings, put_original):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 120), "ROTATED", fontsize=30)
    page.set_rotation(90)
    data = doc.tobytes()
    doc.close()
    ctx = _ctx(settings, put_original, "TL-R", data)
    assert ctx.pages[0].width_px > ctx.pages[0].height_px  # landscape render
    word = textlayer.get_text_layer_page(ctx, 1).page.words[0]
    x, y, w, h = word.bbox
    assert 0 <= x and x + w <= 1 + 1e-9 and 0 <= y and y + h <= 1 + 1e-9
    assert h * ctx.pages[0].height_px > w * ctx.pages[0].width_px  # text now runs vertically


def test_analyzer_on_digital_pdf(settings, put_original):
    ctx = _ctx(settings, put_original, "TL-A", text_pdf_bytes([LINES]))
    outcome = _run(ctx, PdfTextLayerWordsAnalyzer)
    assert outcome.statuses == {"pdf.text_layer_words": "AVAILABLE"}
    cfg = outcome.provenance[0].config
    assert cfg["source"] == "pdf_text_layer" and cfg["words"] >= 5 and cfg["pagesWithText"] == 1


def test_analyzer_not_applicable_to_images(settings, put_original):
    ctx = _ctx(settings, put_original, "TL-I", make_png_bytes(), "original.png")
    assert _run(ctx, PdfTextLayerWordsAnalyzer).statuses == {"pdf.text_layer_words": "NOT_APPLICABLE"}
    with pytest.raises(textlayer.TextLayerUnavailableError):
        textlayer.get_text_layer_page(ctx, 1)


def test_scanned_pdf_has_no_text_layer(settings, put_original):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=make_png_bytes((600, 800)))
    data = doc.tobytes()
    doc.close()
    ctx = _ctx(settings, put_original, "TL-S", data)
    outcome = _run(ctx, PdfTextLayerWordsAnalyzer)
    assert outcome.statuses == {"pdf.text_layer_words": "AVAILABLE"}
    assert outcome.provenance[0].config["words"] == 0 and "no text layer" in outcome.provenance[0].config["reason"]


def test_text_layer_is_computed_once_per_request(settings, put_original, monkeypatch):
    calls = []
    real = textlayer._extract_all
    monkeypatch.setattr(textlayer, "_extract_all", lambda c: calls.append(1) or real(c))
    ctx = _ctx(settings, put_original, "TL-C", text_pdf_bytes([["ONE"], ["TWO"]]))
    _run(ctx, PdfTextLayerWordsAnalyzer)
    first = textlayer.get_text_layer_page(ctx, 1)
    assert textlayer.get_text_layer_page(ctx, 1) is first and textlayer.get_text_layer_page(ctx, 2)
    assert calls == [1]


def test_text_layer_does_not_replace_ocr(settings, put_original, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data",
                        lambda image, **kw: fake_tsv([(1, 1, 1, 1, 100, 50, 200, 40, 90.0, "FromOcr")]))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    ctx = _ctx(settings, put_original, "TL-B", text_pdf_bytes([LINES]))
    outcome = _run(ctx, TesseractOcrAnalyzer, PdfTextLayerWordsAnalyzer)
    assert outcome.statuses == {"ocr": "AVAILABLE", "pdf.text_layer_words": "AVAILABLE"}
    ocr_page = ocr.get_page_ocr(ctx, 1)
    layer = textlayer.get_text_layer_page(ctx, 1)
    assert ocr_page.engine == "tesseract" and [w.text for w in ocr_page.words] == ["FromOcr"]
    assert layer.source == "pdf_text_layer" and "FICTIONAL" in [w.text for w in layer.page.words]
    assert ocr_page is not layer.page


@pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed")
def test_text_layer_boxes_line_up_with_real_ocr_boxes(settings, put_original):
    """Both coordinate systems must describe the same canonical render (real Tesseract)."""
    cfg = Settings(**{**settings.__dict__, "render_dpi": 150})
    ctx = _ctx(cfg, put_original, "TL-X", text_pdf_bytes([LINES]))
    layer = {w.text: w for w in textlayer.get_text_layer_page(ctx, 1).page.words}
    ocr_words = {w.text: w for w in ocr.get_page_ocr(ctx, 1).words}
    shared = [t for t in ("FICTIONAL", "INVOICE", "12345", "Jane", "Example") if t in layer and t in ocr_words]
    assert len(shared) >= 3
    for text in shared:
        a, b = layer[text].bbox, ocr_words[text].bbox
        assert abs((a[0] + a[2] / 2) - (b[0] + b[2] / 2)) < 0.03
        assert abs((a[1] + a[3] / 2) - (b[1] + b[3] / 2)) < 0.03
