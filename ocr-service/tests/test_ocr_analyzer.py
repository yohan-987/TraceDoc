"""OCR analyzer tests with Tesseract MOCKED (pytesseract.image_to_data and engine lookup are faked).

Real Tesseract runs live in test_ocr_real.py.
"""
import pytest

from app import ocr, ocr_core
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.context import AnalysisContext
from app.models import AnalyzeResponse, AnalyzerResult, OcrPage
from app.registry import Analyzer, AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.conftest import make_pdf_bytes, make_png_bytes
from tests.ocr_helpers import fake_tsv

FAKE_ENGINE = ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3", lang="eng")


@pytest.fixture(autouse=True)
def _restore_tesseract_cmd(monkeypatch):
    """ocr_image_file sets pytesseract's global command; put it back after each test."""
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd",
                        ocr_core.pytesseract.pytesseract.tesseract_cmd)


@pytest.fixture
def fake_tesseract(monkeypatch):
    """Pretend Tesseract is installed; count how often OCR actually runs."""
    calls = []

    def image_to_data(image, **kwargs):
        calls.append(kwargs)
        return fake_tsv([
            (1, 1, 1, 1, 100, 50, 200, 40, 96.0, "Hello"),
            (1, 1, 1, 2, 320, 50, 180, 40, 90.0, "World"),
        ])

    monkeypatch.setattr(ocr, "resolve_engine", lambda: FAKE_ENGINE)
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    return calls


def _ctx(settings, put_original, doc_id="O-1", name="original.png", data=None):
    put_original(doc_id, name, data or make_png_bytes((1000, 500)))
    return AnalysisContext(render_document(doc_id, settings), settings)


def _registry(*analyzers):
    registry = AnalyzerRegistry()
    for a in analyzers:
        registry.register(a)
    return registry


def test_ocr_runs_through_analyze_endpoint(make_client, put_original, fake_tesseract):
    put_original("API-1", "original.png", make_png_bytes((1000, 500)))
    client = make_client(_registry(TesseractOcrAnalyzer))
    r = client.post("/analyze", json={"docId": "API-1"})
    assert r.status_code == 200
    body = r.json()
    AnalyzeResponse.model_validate(body)
    assert body["analyzerStatuses"] == {"ocr": "AVAILABLE"}
    prov = body["provenance"][0]
    assert prov["analyzer"] == "ocr" and prov["modelVersion"] == "tesseract 5.5.3"
    assert prov["config"]["words"] == 2 and prov["config"]["lang"] == "eng"
    assert body["fields"] == [] and body["signals"] == []  # OCR alone adds no fields or signals


def test_ocr_page_matches_contract(settings, put_original, fake_tesseract):
    ctx = _ctx(settings, put_original)
    page = ocr.get_page_ocr(ctx, 1)
    assert isinstance(page, OcrPage)
    OcrPage.model_validate(page.model_dump())
    assert page.page == 1 and page.engine == "tesseract" and page.engineVersion == "5.5.3"
    assert (page.widthPx, page.heightPx) == (1000, 500)
    assert page.words[0].bbox == pytest.approx((0.1, 0.1, 0.2, 0.08))
    assert page.lines[0].wordIndices == [0, 1]
    assert fake_tesseract[0]["lang"] == "eng" and "--dpi" in fake_tesseract[0]["config"]


def test_ocr_is_reused_through_context(settings, put_original, fake_tesseract):
    seen = []

    class Consumer(Analyzer):
        name, version = "test.consumer", "1.0"

        def run(self, ctx):
            seen.append(ocr.get_page_ocr(ctx, 1))
            return AnalyzerResult()

    ctx = _ctx(settings, put_original)
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer, Consumer), 10.0)
    assert outcome.statuses == {"ocr": "AVAILABLE", "test.consumer": "AVAILABLE"}
    assert len(fake_tesseract) == 1  # Tesseract ran once for the single page
    assert seen[0] is ocr.get_page_ocr(ctx, 1)  # and everyone gets the same cached object


def test_multi_page_pdf_runs_once_per_page(settings, put_original, fake_tesseract):
    ctx = _ctx(settings, put_original, "O-PDF", "original.pdf", make_pdf_bytes(pages=3))
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    assert len(fake_tesseract) == 3
    assert [ocr.get_page_ocr(ctx, n).page for n in (1, 2, 3)] == [1, 2, 3]


def test_missing_tesseract_is_unavailable(settings, put_original, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    ctx = _ctx(settings, put_original)
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "UNAVAILABLE"}
    assert outcome.provenance[0].status == "UNAVAILABLE"
    assert "TESSERACT_CMD" in outcome.provenance[0].config["reason"]


def test_missing_tesseract_via_api_still_returns_200(make_client, put_original, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    put_original("API-2", "original.png", make_png_bytes())
    r = make_client(_registry(TesseractOcrAnalyzer)).post("/analyze", json={"docId": "API-2"})
    assert r.status_code == 200
    assert r.json()["analyzerStatuses"] == {"ocr": "UNAVAILABLE"}
    assert len(r.json()["pages"]) == 1  # O1 rendering still works


def test_corrupted_page_is_error_not_crash(settings, put_original, fake_tesseract):
    ctx = _ctx(settings, put_original)
    ctx.pages[0].path.write_bytes(b"corrupted")  # damage the canonical render after O1 produced it
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "ERROR"}
    assert "page 1" in outcome.provenance[0].config["reason"]
    assert outcome.provenance[0].config["pagesFailed"] == "1"
    assert fake_tesseract == []  # Tesseract was never reached


def test_one_bad_page_keeps_the_good_pages(settings, put_original, fake_tesseract):
    ctx = _ctx(settings, put_original, "O-2P", "original.pdf", make_pdf_bytes(pages=2))
    ctx.pages[1].path.write_bytes(b"corrupted")
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "ERROR"}  # incomplete OCR is never reported as success
    cfg = outcome.provenance[0].config
    assert cfg["pagesOk"] == 1 and cfg["pagesFailed"] == "2"
    assert ocr.get_page_ocr(ctx, 1).words  # page 1 result is still available to other analyzers
    with pytest.raises(ocr.OcrPageError):
        ocr.get_page_ocr(ctx, 2)


def test_blank_page_is_available_with_no_words(settings, put_original, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_engine", lambda: FAKE_ENGINE)
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", lambda image, **kw: fake_tsv([]))
    ctx = _ctx(settings, put_original)
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    assert outcome.provenance[0].config["words"] == 0
    assert "no text" in outcome.provenance[0].config["reason"]
    page = ocr.get_page_ocr(ctx, 1)
    assert page.words == [] and page.lines == []


def test_tesseract_failure_and_timeout_are_page_errors(settings, put_original, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_engine", lambda: FAKE_ENGINE)

    def boom(image, **kwargs):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", boom)
    ctx = _ctx(settings, put_original)
    outcome = run_analyzers(ctx, _registry(TesseractOcrAnalyzer), 10.0)
    assert outcome.statuses == {"ocr": "ERROR"}
    assert "timed out" in outcome.provenance[0].config["reason"]
