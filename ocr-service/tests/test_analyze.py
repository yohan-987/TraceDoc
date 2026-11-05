import threading

import pytest

import app.models as app_models
from app.context import AnalysisContext
from app.models import AnalyzeResponse, AnalyzerResult, OcrPage
from app.registry import Analyzer
from tests.conftest import make_pdf_bytes, make_png_bytes


class OkAnalyzer(Analyzer):
    name, version = "test.ok", "1.0"

    def run(self, ctx):
        return AnalyzerResult(status="AVAILABLE", model_version="m1", config={"k": 1})


class BoomAnalyzer(Analyzer):
    name, version = "test.boom", "1.0"

    def run(self, ctx):
        raise RuntimeError("kaboom")


class UnavailableAnalyzer(Analyzer):
    name, version = "test.unavailable", "1.0"

    def run(self, ctx):
        return AnalyzerResult.unavailable("model not installed")


class NeverApplies(Analyzer):
    name, version = "test.never", "1.0"

    def applies_to(self, page):
        return False

    def run(self, ctx):  # pragma: no cover
        raise AssertionError("must not run")


def _provenance(body, name):
    return next(p for p in body["provenance"] if p["analyzer"] == name)


def test_contract_models_are_the_shared_ones():
    import analyze_models  # from contracts/
    assert app_models.AnalyzeResponse is analyze_models.AnalyzeResponse
    assert app_models.OcrPage is analyze_models.OcrPage


def test_empty_registry_pdf(client, put_original):
    put_original("CASE-001-DOC-01", "original.pdf", make_pdf_bytes(pages=2))
    r = client.post("/analyze", json={"docId": "CASE-001-DOC-01"})
    assert r.status_code == 200
    body = r.json()
    parsed = AnalyzeResponse.model_validate(body)  # schema-valid
    assert parsed.docId == "CASE-001-DOC-01" and len(parsed.pages) == 2
    assert body["fields"] == [] and body["signals"] == []
    assert body["analyzerStatuses"] == {} and body["provenance"] == []
    assert "docTypeOverriddenBy" not in body["pages"][0]  # absent, not null


def test_empty_registry_png(client, put_original):
    put_original("IMG-001", "original.png", make_png_bytes())
    r = client.post("/analyze", json={"docId": "IMG-001"})
    assert r.status_code == 200
    parsed = AnalyzeResponse.model_validate(r.json())
    assert len(parsed.pages) == 1 and parsed.pages[0].page == 1


def test_unknown_document_is_404(client):
    assert client.post("/analyze", json={"docId": "MISSING"}).status_code == 404


def test_failing_analyzer_does_not_crash_request(make_client, empty_registry, put_original):
    empty_registry.register(BoomAnalyzer)
    put_original("D1", "original.pdf", make_pdf_bytes())
    r = make_client(empty_registry).post("/analyze", json={"docId": "D1"})
    assert r.status_code == 200
    body = r.json()
    assert body["analyzerStatuses"] == {"test.boom": "ERROR"}
    assert "kaboom" in _provenance(body, "test.boom")["config"]["reason"]


def test_failure_does_not_erase_other_results(make_client, empty_registry, put_original):
    for cls in (BoomAnalyzer, OkAnalyzer, UnavailableAnalyzer, NeverApplies):
        empty_registry.register(cls)
    put_original("D2", "original.pdf", make_pdf_bytes())
    body = make_client(empty_registry).post("/analyze", json={"docId": "D2"}).json()
    AnalyzeResponse.model_validate(body)
    assert body["analyzerStatuses"] == {
        "test.boom": "ERROR", "test.never": "NOT_APPLICABLE",
        "test.ok": "AVAILABLE", "test.unavailable": "UNAVAILABLE",
    }
    ok = _provenance(body, "test.ok")
    assert ok["status"] == "AVAILABLE" and ok["modelVersion"] == "m1" and ok["config"]["k"] == 1
    assert _provenance(body, "test.unavailable")["config"]["reason"] == "model not installed"


def test_analyzer_timeout_is_error(make_client, empty_registry, put_original):
    release = threading.Event()

    class Slow(Analyzer):
        name, version = "test.slow", "1.0"

        def run(self, ctx):
            release.wait(10)
            return AnalyzerResult()

    empty_registry.register(Slow)
    empty_registry.register(OkAnalyzer)
    put_original("D3", "original.pdf", make_pdf_bytes())
    try:
        body = make_client(empty_registry, analyzer_timeout_s=0.2).post("/analyze", json={"docId": "D3"}).json()
    finally:
        release.set()
    assert body["analyzerStatuses"]["test.slow"] == "ERROR"
    assert "timed out" in _provenance(body, "test.slow")["config"]["reason"]
    assert body["analyzerStatuses"]["test.ok"] == "AVAILABLE"


def test_bad_analyzer_output_is_error(make_client, empty_registry, put_original):
    class Bad(Analyzer):
        name, version = "test.bad", "1.0"

        def run(self, ctx):
            return {"status": "AVAILABLE"}

    empty_registry.register(Bad)
    put_original("D4", "original.pdf", make_pdf_bytes())
    body = make_client(empty_registry).post("/analyze", json={"docId": "D4"}).json()
    assert body["analyzerStatuses"] == {"test.bad": "ERROR"}


def test_ocr_shared_through_context(settings, put_original):
    from app.render import render_document

    put_original("D5", "original.png", make_png_bytes())
    ctx = AnalysisContext(render_document("D5", settings), settings)
    calls = []

    def compute():
        calls.append(1)
        return OcrPage(docId="D5", page=1, widthPx=200, heightPx=120, renderDpi=100,
                       engine="fake", engineVersion="0", words=[], lines=[])

    first = ctx.get_ocr_page(1, compute)
    second = ctx.get_ocr_page(1, compute)
    assert first is second and len(calls) == 1


def test_duplicate_registration_rejected(empty_registry):
    empty_registry.register(OkAnalyzer)
    with pytest.raises(ValueError):
        empty_registry.register(OkAnalyzer)
