"""PaddleOCR primary / Tesseract fallback. Both engines are FAKED here (no real OCR runs)."""
import pytest

from app import ocr, ocr_core, ocr_paddle
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.context import AnalysisContext
from app.models import AnalyzeResponse, AnalyzerResult, OcrPage
from app.registry import Analyzer, AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.conftest import make_pdf_bytes, make_png_bytes
from tests.ocr_helpers import fake_tsv

FAKE_TESS = ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3", lang="eng")
V3_RESULT = [{
    "rec_texts": ["Hello World", "Second"], "rec_scores": [0.98, 0.9],
    "rec_polys": [[[100, 50], [300, 50], [300, 90], [100, 90]], [[100, 200], [250, 200], [250, 240], [100, 240]]],
}]


class FakePaddle:
    def __init__(self, error=None, result=None):
        self.calls, self.error, self.result = 0, error, V3_RESULT if result is None else result

    def predict(self, path):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def tess_calls(monkeypatch):
    """Mock Tesseract (installed + counting calls) and select the 'auto' engine policy."""
    calls = []

    def image_to_data(image, **kwargs):
        calls.append(1)
        return fake_tsv([(1, 1, 1, 1, 100, 50, 200, 40, 90.0, "Fallback")])

    monkeypatch.setenv("OCR_ENGINE", "auto")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: FAKE_TESS)
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    return calls


def install_paddle(monkeypatch, engine=None, unavailable=None):
    created = []

    def create():
        created.append(1)
        if unavailable:
            raise ocr_paddle.PaddleUnavailable(unavailable)
        return engine, "3.0.0"

    monkeypatch.setattr(ocr_paddle, "_create_engine", create)
    return created


def _ctx(settings, put_original, doc="E-1", pages=1):
    if pages == 1:
        put_original(doc, "original.png", make_png_bytes((1000, 500)))
    else:
        put_original(doc, "original.pdf", make_pdf_bytes(pages=pages))
    return AnalysisContext(render_document(doc, settings), settings)


def _run(ctx, *extra):
    registry = AnalyzerRegistry()
    for a in (TesseractOcrAnalyzer, *extra):
        registry.register(a)
    return run_analyzers(ctx, registry, 30.0)


def test_paddleocr_success(settings, put_original, tess_calls, monkeypatch):
    paddle = FakePaddle()
    install_paddle(monkeypatch, paddle)
    ctx = _ctx(settings, put_original)
    outcome = _run(ctx)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    page = ocr.get_page_ocr(ctx, 1)
    OcrPage.model_validate(page.model_dump())
    assert page.engine == "paddleocr" and page.engineVersion == "3.0.0"
    assert [w.text for w in page.words] == ["Hello", "World", "Second"]
    assert page.lines[0].bbox == pytest.approx((0.1, 0.1, 0.2, 0.08))
    assert tess_calls == []  # fallback was not needed
    prov = outcome.provenance[0]
    assert prov.modelVersion == "paddleocr 3.0.0" and prov.config["engines"] == "paddleocr"


def test_paddle_unavailable_falls_back_to_tesseract(settings, put_original, tess_calls, monkeypatch):
    install_paddle(monkeypatch, unavailable="PaddleOCR is not installed")
    ctx = _ctx(settings, put_original)
    outcome = _run(ctx)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    page = ocr.get_page_ocr(ctx, 1)
    assert page.engine == "tesseract" and [w.text for w in page.words] == ["Fallback"]
    assert outcome.provenance[0].config["engines"] == "tesseract"


def test_paddle_failure_falls_back_and_is_not_retried(settings, put_original, tess_calls, monkeypatch):
    paddle = FakePaddle(error=RuntimeError("inference blew up"))
    install_paddle(monkeypatch, paddle)
    ctx = _ctx(settings, put_original, "E-3P", pages=3)
    outcome = _run(ctx)
    assert outcome.statuses == {"ocr": "AVAILABLE"}
    assert all(ocr.get_page_ocr(ctx, n).engine == "tesseract" for n in (1, 2, 3))
    assert paddle.calls == 1      # first failure disables PaddleOCR for the rest of the request
    assert len(tess_calls) == 3   # every page still got OCR


def test_unparseable_paddle_output_falls_back(settings, put_original, tess_calls, monkeypatch):
    install_paddle(monkeypatch, FakePaddle(result=[object()]))
    ctx = _ctx(settings, put_original)
    _run(ctx)
    assert ocr.get_page_ocr(ctx, 1).engine == "tesseract"


def test_paddle_engine_created_once_and_reused(settings, put_original, tess_calls, monkeypatch):
    paddle = FakePaddle()
    created = install_paddle(monkeypatch, paddle)
    ctx = _ctx(settings, put_original, "E-2P", pages=2)
    _run(ctx)
    ctx2 = AnalysisContext(render_document("E-2P", settings), settings)  # a second request
    _run(ctx2)
    assert created == [1]       # initialised once for the whole process
    assert paddle.calls == 4    # but run per page


def test_ocr_reused_from_context_with_paddle(settings, put_original, tess_calls, monkeypatch):
    paddle = FakePaddle()
    install_paddle(monkeypatch, paddle)
    seen = []

    class Consumer(Analyzer):
        name, version = "test.consumer", "1.0"

        def run(self, ctx):
            seen.append(ocr.get_page_ocr(ctx, 1))
            return AnalyzerResult()

    ctx = _ctx(settings, put_original)
    outcome = _run(ctx, Consumer)
    assert outcome.statuses == {"ocr": "AVAILABLE", "test.consumer": "AVAILABLE"}
    assert paddle.calls == 1 and seen[0] is ocr.get_page_ocr(ctx, 1)


def test_policy_tesseract_never_touches_paddle(settings, put_original, tess_calls, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    created = install_paddle(monkeypatch, FakePaddle())
    ctx = _ctx(settings, put_original)
    _run(ctx)
    assert created == [] and ocr.get_page_ocr(ctx, 1).engine == "tesseract"


def test_policy_paddleocr_has_no_fallback(settings, put_original, tess_calls, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "paddleocr")
    install_paddle(monkeypatch, unavailable="not installed")
    outcome = _run(_ctx(settings, put_original))
    assert outcome.statuses == {"ocr": "UNAVAILABLE"} and tess_calls == []

    ocr_paddle.reset_paddle()
    install_paddle(monkeypatch, FakePaddle(error=RuntimeError("boom")))
    outcome = _run(_ctx(settings, put_original, "E-4"))
    assert outcome.statuses == {"ocr": "ERROR"} and tess_calls == []


def test_both_engines_unavailable(settings, put_original, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "auto")
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    install_paddle(monkeypatch, unavailable="PaddleOCR is not installed")
    outcome = _run(_ctx(settings, put_original))
    assert outcome.statuses == {"ocr": "UNAVAILABLE"}
    reason = outcome.provenance[0].config["reason"]
    assert "PaddleOCR" in reason and "Tesseract" in reason


def test_corrupted_image_is_not_retried_with_the_fallback(settings, put_original, tess_calls, monkeypatch):
    paddle = FakePaddle()
    install_paddle(monkeypatch, paddle)
    ctx = _ctx(settings, put_original)
    ctx.pages[0].path.write_bytes(b"corrupted")
    outcome = _run(ctx)
    assert outcome.statuses == {"ocr": "ERROR"} and paddle.calls == 0 and tess_calls == []


def test_analyze_endpoint_with_paddle(make_client, put_original, tess_calls, monkeypatch):
    install_paddle(monkeypatch, FakePaddle())
    put_original("E-API", "original.png", make_png_bytes((1000, 500)))
    registry = AnalyzerRegistry()
    registry.register(TesseractOcrAnalyzer)
    body = make_client(registry).post("/analyze", json={"docId": "E-API"}).json()
    AnalyzeResponse.model_validate(body)
    assert body["analyzerStatuses"] == {"ocr": "AVAILABLE"}
    assert body["provenance"][0]["config"]["engines"] == "paddleocr"
    assert body["fields"] == [] and body["signals"] == []
