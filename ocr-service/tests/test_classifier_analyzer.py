"""page.classifier plugin: context integration, statuses, provenance, multi-page and /analyze."""
import json

import pytest
from PIL import Image

from app import ocr, ocr_core
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.classifier_config import DEFAULT_PATH, load_classifier_config
from app.classify import apply_page_classifications, classification_store
from app.context import AnalysisContext
from app.models import (AnalyzeResponse, PageRecord, RenderedDocument, RenderedPage)
from app.ocr_fixtures import seed_ocr
from app.registry import AnalyzerRegistry, run_analyzers
from tests import classifier_pages as P
from tests.ocr_helpers import fake_tsv

ID_SIZE, PAGE_SIZE = (1011, 638), (1240, 1754)


def make_ctx(settings, tmp_path, sizes, files=False, doc_id="CL-1"):
    pages = []
    for n, (w, h) in enumerate(sizes, 1):
        path = tmp_path / f"page-{n}.png"
        if files:
            Image.new("RGB", (w, h), "white").save(path)
        record = PageRecord(docId=doc_id, page=n, renderPath=f"derived/{doc_id}/page-{n}.png", renderDpi=150.0,
                            rotationDeg=0.0, deskewDeg=0.0, docType="UNKNOWN", docTypeConfidence=0.0)
        pages.append(RenderedPage(record=record, path=path, width_px=w, height_px=h))
    return AnalysisContext(RenderedDocument(doc_id, tmp_path / "original.png", "0" * 64, pages), settings)


def registry(*analyzers):
    reg = AnalyzerRegistry()
    for a in analyzers:
        reg.register(a)
    return reg


def tsv_from_page(page, width, height):
    """TSV rows a fake Tesseract would return for an OcrPage (normalized boxes -> pixels)."""
    rows = []
    for li, line in enumerate(page.lines, 1):
        for wn, idx in enumerate(line.wordIndices, 1):
            w = page.words[idx]
            x, y, bw, bh = w.bbox
            rows.append((1, 1, li, wn, round(x * width), round(y * height), round(bw * width), round(bh * height),
                         95.0, w.text))
    return fake_tsv(rows)


def provenance(outcome, name="page.classifier"):
    return next(p for p in outcome.provenance if p.analyzer == name)


MIXED = [(P.id_card, ID_SIZE), (P.application_form, PAGE_SIZE), (P.bank_statement, PAGE_SIZE),
         (P.other_invoice, PAGE_SIZE), (P.blank_page, PAGE_SIZE)]
EXPECTED = ["id_card", "application_form", "bank_statement", "other", "UNKNOWN"]


def test_multi_page_document_is_classified_page_by_page(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [size for _, size in MIXED])
    seed_ocr(ctx, {n: make() for n, (make, _) in enumerate(MIXED, 1)})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"page.classifier": "AVAILABLE"}
    store = classification_store(ctx)
    assert [store[n].docType for n in range(1, 6)] == EXPECTED  # different types in one document
    records = apply_page_classifications(ctx, [p.record for p in ctx.pages])
    assert [r.docType for r in records] == EXPECTED
    assert all(0.0 <= r.docTypeConfidence <= 1.0 for r in records)
    assert records[4].docTypeConfidence == 0.0  # the blank page


def test_pages_do_not_influence_each_other(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [size for _, size in reversed(MIXED)])
    seed_ocr(ctx, {n: make() for n, (make, _) in enumerate(reversed(MIXED), 1)})
    run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert [classification_store(ctx)[n].docType for n in range(1, 6)] == list(reversed(EXPECTED))


def test_provenance_contains_configured_thresholds_and_evidence(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE, PAGE_SIZE])
    seed_ocr(ctx, {1: P.bank_statement(), 2: P.blank_page()})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    prov = provenance(outcome)
    cfg = load_classifier_config()
    assert prov.status == "AVAILABLE" and prov.analyzerVersion == "0.1"
    for key, value in cfg.as_provenance().items():
        assert prov.config[key] == value  # every configured threshold is echoed, unchanged
    assert prov.config["cfg.minScore"] == cfg.minScore and prov.config["cfg.ambiguityMargin"] == cfg.ambiguityMargin
    assert prov.config["page1.docType"] == "bank_statement" and prov.config["page2.docType"] == "UNKNOWN"
    assert "table_rows" in prov.config["page1.evidence"] and "bank_statement=" in prov.config["page1.scores"]
    assert "too little text" in prov.config["page2.note"]
    assert prov.config["count.bank_statement"] == 1 and prov.config["pagesClassified"] == 2
    assert all(isinstance(v, (int, float, str)) for v in prov.config.values())


def test_provenance_follows_a_custom_config_file(settings, tmp_path, monkeypatch):
    data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    data.update(minScore=0.99, ambiguityMargin=0.2)
    path = tmp_path / "strict.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLASSIFIER_CONFIG", str(path))
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE])
    seed_ocr(ctx, {1: P.other_letter()})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert provenance(outcome).config["cfg.minScore"] == 0.99
    assert classification_store(ctx)[1].docType == "UNKNOWN"  # the stricter threshold changed the result


def test_recorded_ocr_needs_no_ocr_engine(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")  # no engine at all
    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: P.id_card()})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"page.classifier": "AVAILABLE"}


def test_unavailable_ocr_is_reported_not_guessed(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE, PAGE_SIZE])
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"page.classifier": "UNAVAILABLE"}
    assert "OCR is not available" in provenance(outcome).config["reason"]
    assert classification_store(ctx) == {}
    records = apply_page_classifications(ctx, [p.record for p in ctx.pages])
    assert [(r.docType, r.docTypeConfidence) for r in records] == [("UNKNOWN", 0.0)] * 2  # untouched


def test_failed_ocr_page_does_not_hide_the_others(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data",
                        lambda image, **kw: tsv_from_page(P.application_form(), *image.size))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE, PAGE_SIZE], files=True)
    ctx.pages[1].path.write_bytes(b"corrupted")
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"page.classifier": "ERROR"}
    assert provenance(outcome).config["pagesFailed"] == "2"
    assert classification_store(ctx)[1].docType == "application_form" and 2 not in classification_store(ctx)
    assert apply_page_classifications(ctx, [p.record for p in ctx.pages])[1].docType == "UNKNOWN"


def test_missing_config_is_an_error_status_not_a_crash(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_CONFIG", str(tmp_path / "missing.json"))
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE])
    seed_ocr(ctx, {1: P.bank_statement()})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"page.classifier": "ERROR"}
    assert "not found" in provenance(outcome).config["reason"]


def test_classifier_reuses_live_ocr_without_recomputing(settings, tmp_path, monkeypatch):
    pages = [P.application_form(), P.bank_statement(), P.other_invoice()]
    calls = []

    def image_to_data(image, **kwargs):
        calls.append(1)
        return tsv_from_page(pages[len(calls) - 1], *image.size)

    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE] * 3, files=True)
    outcome = run_analyzers(ctx, registry(TesseractOcrAnalyzer, PageClassifierAnalyzer), 30.0)
    assert outcome.statuses == {"ocr": "AVAILABLE", "page.classifier": "AVAILABLE"}
    assert len(calls) == 3  # one OCR run per page in total, the classifier added none
    assert [classification_store(ctx)[n].docType for n in (1, 2, 3)] == ["application_form", "bank_statement", "other"]


def test_apply_keeps_overrides_and_unset_optional_fields(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE, PAGE_SIZE])
    seed_ocr(ctx, {1: P.bank_statement(), 2: P.other_invoice()})
    run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    base = [p.record for p in ctx.pages]
    base[1] = PageRecord(**{**base[1].model_dump(), "docTypeOverriddenBy": "analyst-1"})
    updated = apply_page_classifications(ctx, base)
    assert "docTypeOverriddenBy" not in updated[0].model_dump(exclude_unset=True)  # stays absent, not null
    assert updated[1].docTypeOverriddenBy == "analyst-1"


def test_response_with_classified_pages_matches_the_contract(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [size for _, size in MIXED])
    seed_ocr(ctx, {n: make() for n, (make, _) in enumerate(MIXED, 1)})
    outcome = run_analyzers(ctx, registry(PageClassifierAnalyzer), 30.0)
    response = AnalyzeResponse(
        docId=ctx.doc_id, pipelineVersion="test", pages=apply_page_classifications(ctx, [p.record for p in ctx.pages]),
        fields=outcome.fields, signals=outcome.signals, analyzerStatuses=outcome.statuses, provenance=outcome.provenance)
    body = response.model_dump(mode="json", exclude_unset=True)
    AnalyzeResponse.model_validate(body)
    assert [p["docType"] for p in body["pages"]] == EXPECTED
    assert body["fields"] == [] and body["signals"] == []


# ---- through the real /analyze route (needs FastAPI + PyMuPDF)

def _patch_tesseract(monkeypatch, pages):
    calls = []

    def image_to_data(image, **kwargs):
        calls.append(1)
        return tsv_from_page(pages[len(calls) - 1], *image.size)

    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    return calls


def test_analyze_endpoint_classifies_every_pdf_page(make_client, put_original, monkeypatch):
    from tests.conftest import make_pdf_bytes
    pages = [P.application_form(), P.bank_statement(), P.other_invoice()]
    calls = _patch_tesseract(monkeypatch, pages)
    put_original("CL-API", "original.pdf", make_pdf_bytes(pages=3))
    client = make_client(registry(TesseractOcrAnalyzer, PageClassifierAnalyzer))
    r = client.post("/analyze", json={"docId": "CL-API"})
    assert r.status_code == 200
    body = r.json()
    AnalyzeResponse.model_validate(body)
    assert [p["docType"] for p in body["pages"]] == ["application_form", "bank_statement", "other"]
    assert all(0 <= p["docTypeConfidence"] <= 1 for p in body["pages"])
    assert body["analyzerStatuses"] == {"ocr": "AVAILABLE", "page.classifier": "AVAILABLE"}
    prov = next(p for p in body["provenance"] if p["analyzer"] == "page.classifier")
    assert prov["config"]["cfg.minScore"] == load_classifier_config().minScore
    assert len(calls) == 3


def test_analyze_endpoint_blank_page_is_unknown(make_client, put_original, monkeypatch):
    from tests.conftest import make_png_bytes
    _patch_tesseract(monkeypatch, [P.blank_page()])
    put_original("CL-BLANK", "original.png", make_png_bytes((1240, 1754)))
    client = make_client(registry(TesseractOcrAnalyzer, PageClassifierAnalyzer))
    body = client.post("/analyze", json={"docId": "CL-BLANK"}).json()
    assert body["pages"][0]["docType"] == "UNKNOWN" and body["pages"][0]["docTypeConfidence"] == 0.0
    assert body["analyzerStatuses"]["page.classifier"] == "AVAILABLE"


def test_analyze_endpoint_without_the_classifier_is_unchanged(make_client, put_original):
    from tests.conftest import make_png_bytes
    put_original("CL-NONE", "original.png", make_png_bytes())
    body = make_client(AnalyzerRegistry()).post("/analyze", json={"docId": "CL-NONE"}).json()
    assert body["pages"][0]["docType"] == "UNKNOWN" and body["analyzerStatuses"] == {}
