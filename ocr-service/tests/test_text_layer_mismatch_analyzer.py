"""O6 analyzer (pdf.textlayer): digital PDFs against a simulated OCR of what the page really shows.

The simulated OCR is derived from the PDF's own text layer minus the hidden words, so these tests need no OCR engine;
the real engine is exercised in test_text_layer_mismatch_samples.py.
"""
import io

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

import app.analyzers.pdf_text_layer_mismatch as mod
from app import textlayer
from app.analyzers.pdf_text_layer_mismatch import PdfTextLayerMismatchAnalyzer
from app.context import AnalysisContext
from app.models import ForensicSignal, OcrPage
from app.ocr_core import EngineStatus, OcrPageError, OcrUnavailableError
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.conftest import make_png_bytes

LINES = ["ACCOUNT STATEMENT", "Account Number 12345678", "Opening Balance 1,250.00", "Closing Balance 2,850.00",
         "Customer Reference 99871", "Branch Central Office"]


def pdf(hidden=None, pages=1, cover=False, image_patch=None):
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        for i, text in enumerate(LINES):
            page.insert_text((60, 100 + i * 60), f"{text}", fontsize=22)
        if hidden and (not isinstance(hidden, dict) or n in hidden):
            spec = hidden if not isinstance(hidden, dict) else hidden[n]
            kind = spec
            at = (330, 100 + 3 * 60)  # to the right of the closing balance line
            if kind == "white":
                page.insert_text(at, "USD 99,999.00", fontsize=22, color=(1, 1, 1))
            elif kind == "invisible":
                page.insert_text(at, "USD 99,999.00", fontsize=22, render_mode=3)
            elif kind == "covered":
                page.insert_text(at, "USD 99,999.00", fontsize=22)
                page.draw_rect(pymupdf.Rect(325, 258, 560, 288), color=None, fill=(1, 1, 1))  # over the hidden words
        if image_patch:
            img = Image.new("RGB", (600, 70), "white")
            ImageDraw.Draw(img).text((8, 6), image_patch, fill="black", font=ImageFont.load_default(size=52))
            buf = io.BytesIO(); img.save(buf, "PNG")
            page.insert_image(pymupdf.Rect(322, 236, 560, 262), stream=buf.getvalue())
    data = doc.tobytes(); doc.close()
    return data


def simulated_ocr(visible_pdf_bytes, tmp_path, settings):
    """OCR pages as a perfect reader of the visible page would deliver them: the text layer of the PDF WITHOUT its hidden words."""
    (settings.evidence_dir / "vis").mkdir(parents=True, exist_ok=True)
    (settings.evidence_dir / "vis" / "original.pdf").write_bytes(visible_pdf_bytes)
    ctx = AnalysisContext(render_document("vis", settings), settings)
    return {n: textlayer.get_text_layer_page(ctx, n).page.model_copy(update={"engine": "simulated-ocr", "docId": "x"}) for n in range(1, len(ctx.pages) + 1)}


@pytest.fixture
def run(settings, put_original, monkeypatch, tmp_path):
    def go(data, visible=None, ocr_override=None, engine_available=False, doc="D1", name="original.pdf"):
        put_original(doc, name, data)
        ctx = AnalysisContext(render_document(doc, settings), settings)
        pages = ocr_override if ocr_override is not None else simulated_ocr(visible if visible is not None else pdf(), tmp_path, settings)

        def fake_ocr(c, page, timeout_s=None):
            if isinstance(pages, Exception):
                raise pages
            if isinstance(pages, dict) and isinstance(pages.get(page), Exception):
                raise pages[page]
            return pages[page].model_copy(update={"docId": c.doc_id})

        monkeypatch.setattr(mod, "get_page_ocr", fake_ocr)
        monkeypatch.setattr(mod, "get_engine", lambda c: EngineStatus(available=engine_available, reason="not in tests", cmd=None, version=None, lang="eng"))
        reg = AnalyzerRegistry(); reg.register(PdfTextLayerMismatchAnalyzer)
        return run_analyzers(ctx, reg, 60.0), ctx
    return go


def test_a_genuine_pdf_yields_no_signal(run):
    out, _ = run(pdf())
    assert out.statuses == {"pdf.textlayer": "AVAILABLE"} and out.signals == []
    config = out.provenance[0].config
    assert config["pagesCompared"] == 1 and config["signals"] == 0 and config["cfg.textSimilarityMin"] == 0.6


@pytest.mark.parametrize("technique", ["white", "invisible", "covered"])
def test_hidden_text_is_reported_once_with_the_hidden_text_and_its_box(run, technique):
    out, ctx = run(pdf(technique))
    assert out.statuses["pdf.textlayer"] == "AVAILABLE" and len(out.signals) == 1
    sig: ForensicSignal = out.signals[0]
    assert sig.detector == "pdf.textlayer" and sig.type == "TEXT_LAYER_MISMATCH" and 0 < sig.confidence <= 1
    assert sig.measurements["kind"] == "layer_only" and sig.measurements["textLayerText"] == "USD 99,999.00"
    assert sig.measurements["renderedText"] == "" and sig.measurements["differingChars"] == 10   # every letter and digit of "USD 99,999.00"
    assert len(sig.regions) == 1 and sig.regions[0].docId == "D1" and sig.regions[0].page == 1
    x, y, w, h = sig.regions[0].bbox
    assert 330 / 595 - 0.03 < x < 330 / 595 + 0.03 and 0.25 < y < 0.40 and w > 0.2    # right of the closing balance line
    assert sig.provenance.analyzer == "pdf.textlayer" and sig.provenance.config["page"] == 1 and sig.provenance.inputSha256 == ctx.input_sha256
    assert sig.signalId == "D1-tlmm-p1-01"


def test_signal_shape_matches_the_recorded_fixture_keys(run):
    out, _ = run(pdf("white"))
    m = out.signals[0].measurements
    assert {"renderedText", "textLayerText", "differingChars"} <= set(m)         # names used by contracts/fixtures/analyze/D-03.json
    assert all(isinstance(v, (int, float, str)) and not isinstance(v, bool) for v in m.values())


def test_text_that_the_page_shows_differently_is_a_two_region_signal(run, settings, tmp_path):
    tampered = pdf(image_patch="USD 51,500.00")                        # layer: the printed lines; page: patched value
    # the visible page shows the patch text where the layer has nothing readable: simulate OCR reading the patch
    base = simulated_ocr(pdf(), tmp_path, settings)
    layer_words = base[1].words
    closing = next(i for i, w in enumerate(layer_words) if w.text == "2,850.00")
    new_words = list(layer_words)
    new_words[closing] = new_words[closing].model_copy(update={"text": "5,850.00"})   # visible reading of the same spot differs
    ocr = {1: base[1].model_copy(update={"words": new_words})}
    out, _ = run(pdf(), ocr_override=ocr)
    assert [s.measurements["kind"] for s in out.signals] == ["differs"]
    sig = out.signals[0]
    assert sig.measurements["textLayerText"] == "2,850.00" and sig.measurements["renderedText"] == "5,850.00" and sig.measurements["differingChars"] == 1
    assert [r.label for r in sig.regions] == ["what the text layer says", "what the page shows"]
    assert sig.provenance.config["page"] == 1 and out.provenance[0].config["differsUnverified"] == 1   # no second reading available here


def test_only_the_tampered_page_of_a_multi_page_pdf_is_flagged(run, settings, tmp_path):
    data = pdf(hidden={1: "white"}, pages=3)
    visible = pdf(pages=3)
    out, _ = run(data, visible=visible)
    assert [s.provenance.config["page"] for s in out.signals] == [2]
    assert out.provenance[0].config["pagesCompared"] == 3
    assert len({s.signalId for s in out.signals}) == len(out.signals)


def test_image_originals_are_not_applicable(run):
    out, _ = run(make_png_bytes(), name="original.png", ocr_override={})
    assert out.statuses == {"pdf.textlayer": "NOT_APPLICABLE"}


def test_a_pdf_without_a_text_layer_is_not_applicable(run, settings):
    doc = pymupdf.open(); page = doc.new_page(width=595, height=842)
    buf = io.BytesIO(); Image.new("RGB", (300, 200), "white").save(buf, "PNG"); page.insert_image(pymupdf.Rect(50, 50, 350, 250), stream=buf.getvalue())
    data = doc.tobytes(); doc.close()
    out, _ = run(data, ocr_override={})
    assert out.statuses == {"pdf.textlayer": "NOT_APPLICABLE"} and out.signals == []


def test_unreadable_page_is_unavailable_not_clean(run):
    out, _ = run(pdf(), ocr_override=OcrUnavailableError("no engine"))
    assert out.statuses == {"pdf.textlayer": "UNAVAILABLE"} and out.signals == []


def test_a_page_whose_ocr_fails_is_an_error_naming_the_page(run, settings, tmp_path):
    base = simulated_ocr(pdf(pages=2), tmp_path, settings)
    out, _ = run(pdf(pages=2), ocr_override={1: base[1], 2: OcrPageError("timed out")})
    assert out.statuses["pdf.textlayer"] == "ERROR" and out.signals == []
    assert out.provenance[0].config["pagesFailed"] == "2"


def test_ocr_that_read_too_little_is_unavailable_with_the_reason(run, settings, tmp_path):
    base = simulated_ocr(pdf(), tmp_path, settings)
    out, _ = run(pdf(), ocr_override={1: base[1].model_copy(update={"words": base[1].words[:1], "lines": []})})
    assert out.statuses["pdf.textlayer"] == "UNAVAILABLE" and out.signals == []


def test_hidden_words_are_still_found_when_ocr_read_too_little(run, settings, tmp_path):
    base = simulated_ocr(pdf(), tmp_path, settings)
    out, _ = run(pdf("white"), ocr_override={1: base[1].model_copy(update={"words": base[1].words[:1], "lines": []})})
    assert out.statuses["pdf.textlayer"] == "AVAILABLE" and out.signals and out.provenance[0].config["pagesNotComparable"] == "1"


def test_bad_config_is_an_error_not_a_crash(run, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"; bad.write_text("{}")
    monkeypatch.setenv("TEXT_LAYER_CHECK_CONFIG", str(bad))
    out, _ = run(pdf())
    assert out.statuses == {"pdf.textlayer": "ERROR"}
