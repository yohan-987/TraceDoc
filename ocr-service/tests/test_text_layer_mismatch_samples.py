"""O6 against the repo's D1/D2 sample PDFs with a real OCR engine (skipped without Tesseract or without samples/).

Recorded results, not aspirations: the two hidden-text samples give exactly one signal each, every other sample PDF
(genuine, controls, the other tampered PDFs, dev and test scenarios) gives none. If a threshold change breaks that, the
change is wrong or this file and the README must say so; do not widen tests to make them pass.
"""
import json
from pathlib import Path

import pytest

from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.pdf_text_layer_mismatch import PdfTextLayerMismatchAnalyzer
from app.context import AnalysisContext
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.ocr_helpers import tesseract_available

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
pytestmark = [pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed"),
              pytest.mark.skipif(not (SAMPLES / "tampered" / "ground_truth.json").exists(), reason="samples/ not present")]

HIDDEN = {"T-08": "USD 56,500.00", "T-09": "1969-12-31"}
ALL_PDFS = sorted(p for p in SAMPLES.rglob("*.pdf") if not any(part in ("d1-lite", "engine_check") for part in p.parts))
_CACHE = {}


def truth():
    return {e["id"]: e for e in json.loads((SAMPLES / "tampered" / "ground_truth.json").read_text())["entries"]}


def iou(a, b):
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def analyse(settings, put_original, path):
    if path not in _CACHE:
        doc = "TL-" + str(len(_CACHE))
        put_original(doc, "original.pdf", path.read_bytes())
        ctx = AnalysisContext(render_document(doc, settings), settings)
        registry = AnalyzerRegistry()
        registry.register(TesseractOcrAnalyzer)
        registry.register(PdfTextLayerMismatchAnalyzer)
        _CACHE[path] = run_analyzers(ctx, registry, 120.0)
    return _CACHE[path]


@pytest.mark.parametrize("entry_id", sorted(HIDDEN))
def test_hidden_text_samples_give_one_signal_on_the_hidden_text(settings, put_original, entry_id):
    entry = truth()[entry_id]
    out = analyse(settings, put_original, SAMPLES / entry["file"])
    assert out.statuses["pdf.textlayer"] == "AVAILABLE"
    assert len(out.signals) == 1
    sig = out.signals[0]
    assert sig.type == "TEXT_LAYER_MISMATCH" and sig.detector == "pdf.textlayer" and sig.measurements["kind"] == "layer_only"
    assert sig.measurements["textLayerText"] == HIDDEN[entry_id] and sig.measurements["renderedText"] == ""
    assert sig.regions[0].page == entry["page"]
    assert iou(sig.regions[0].bbox, tuple(entry["bbox"])) > 0.5


NO_SIGNAL = [p for p in ALL_PDFS if "hidden_" not in p.name]


@pytest.mark.parametrize("path", NO_SIGNAL, ids=lambda p: "/".join(p.parts[-3:]))
def test_every_other_sample_pdf_gives_no_signal(settings, put_original, path):
    out = analyse(settings, put_original, path)
    assert out.statuses["pdf.textlayer"] == "AVAILABLE", out.provenance[-1].config.get("reason")
    assert out.signals == [], [(s.measurements["kind"], s.measurements["textLayerText"], s.measurements["renderedText"]) for s in out.signals]


def test_the_analyzer_runs_inside_the_real_service(settings, put_original, make_client):
    from app.registry import discover_analyzers
    entry = truth()["T-08"]
    put_original("TL-API", "original.pdf", (SAMPLES / entry["file"]).read_bytes())
    body = make_client(discover_analyzers()).post("/analyze", json={"docId": "TL-API"}).json()
    assert body["analyzerStatuses"]["pdf.textlayer"] == "AVAILABLE"
    signals = [s for s in body["signals"] if s["detector"] == "pdf.textlayer"]
    assert len(signals) == 1 and signals[0]["type"] == "TEXT_LAYER_MISMATCH"
    assert any(p["analyzer"] == "pdf.textlayer" for p in body["provenance"])


def _searchable_scan(extra_line=False):
    """A picture of a page plus an invisible OCR-style text layer over the same words (what scanners and OCR tools produce)."""
    import io

    import pymupdf
    from PIL import Image, ImageDraw, ImageFont
    lines = ["ACCOUNT STATEMENT", "Account Number 12345678", "Opening Balance 1,250.00", "Closing Balance 2,850.00"]
    img = Image.new("RGB", (1190, 1684), "white")
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        draw.text((120, 150 + i * 120), text, fill="black", font=ImageFont.load_default(size=44))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    for i, text in enumerate(lines):
        page.insert_text((60, 93 + i * 60), text, fontsize=22, render_mode=3)
    if extra_line:
        page.insert_text((60, 520), "Approved credit limit 500,000.00", fontsize=22, render_mode=3)
    return doc.tobytes()


def test_a_searchable_scan_with_a_matching_invisible_layer_is_clean(settings, put_original, tmp_path):
    put_original("TL-SCAN", "original.pdf", _searchable_scan())
    ctx = AnalysisContext(render_document("TL-SCAN", settings), settings)
    registry = AnalyzerRegistry(); registry.register(TesseractOcrAnalyzer); registry.register(PdfTextLayerMismatchAnalyzer)
    out = run_analyzers(ctx, registry, 120.0)
    assert out.statuses["pdf.textlayer"] == "AVAILABLE" and out.signals == []


def test_an_extra_invisible_line_in_a_searchable_scan_is_found(settings, put_original):
    put_original("TL-SCAN2", "original.pdf", _searchable_scan(extra_line=True))
    ctx = AnalysisContext(render_document("TL-SCAN2", settings), settings)
    registry = AnalyzerRegistry(); registry.register(TesseractOcrAnalyzer); registry.register(PdfTextLayerMismatchAnalyzer)
    out = run_analyzers(ctx, registry, 120.0)
    assert [s.measurements["textLayerText"] for s in out.signals] == ["Approved credit limit 500,000.00"]
