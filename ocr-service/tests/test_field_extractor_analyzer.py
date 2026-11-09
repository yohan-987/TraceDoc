"""page.field_extractor plugin: registration, context/classifier integration, statuses, failure isolation, /analyze."""
import shutil

import pytest

from app import extract_schema, ocr, ocr_core
from app.analyzers.field_extractor import FieldExtractorAnalyzer, extraction_store
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.classify import classification_store
from app.models import AnalyzeResponse, AnalyzerResult, ExtractedField
from app.ocr_fixtures import seed_ocr
from app.registry import Analyzer, AnalyzerRegistry, default_registry, discover_analyzers, run_analyzers
from tests import classifier_pages as P
from tests import extractor_pages as X
from tests.test_classifier_analyzer import make_ctx, tsv_from_page

ID_SIZE, PAGE_SIZE = (1011, 638), (1240, 1754)
NAME = "page.field_extractor"


def registry(*analyzers):
    reg = AnalyzerRegistry()
    for a in analyzers:
        reg.register(a)
    return reg


def prov(outcome, name=NAME):
    return next(p for p in outcome.provenance if p.analyzer == name)


def run_both(ctx):
    return run_analyzers(ctx, registry(PageClassifierAnalyzer, FieldExtractorAnalyzer), 30.0)


def test_plugin_is_registered_through_the_existing_registry():
    discover_analyzers()
    assert NAME in [a.name for a in default_registry.analyzers()]
    assert NAME > "page.classifier"          # runs after the classifier in name order


def test_fields_are_contract_objects_for_every_document_type(settings, tmp_path):
    pages = [(X.id_card, ID_SIZE), (X.application_form, PAGE_SIZE), (X.bank_statement, PAGE_SIZE)]
    ctx = make_ctx(settings, tmp_path, [size for _, size in pages])
    seed_ocr(ctx, {n: make() for n, (make, _) in enumerate(pages, 1)})
    outcome = run_both(ctx)
    assert outcome.statuses == {"page.classifier": "AVAILABLE", NAME: "AVAILABLE"}
    assert len(outcome.fields) == 4 + 8 + 5
    assert [f.fieldId for f in outcome.fields] == [f"FLD-{i:03d}" for i in range(1, 18)]
    for f in outcome.fields:
        assert isinstance(f, ExtractedField)
        ExtractedField.model_validate(f.model_dump())
        assert f.docId == ctx.doc_id and f.source == "ocr" and f.region.docId == ctx.doc_id and f.region.label == f.name
        assert f.region.page in (1, 2, 3)                              # 1-based
        assert 0 <= f.ocrConfidence <= 1 and 0 <= f.extractionConfidence <= 1
        assert f.validationStatus in ("VALID", "INVALID_FORMAT", "NOT_CHECKED")
    by_page = {}
    for f in outcome.fields:
        by_page.setdefault(f.region.page, set()).add(f.name)
    assert by_page[1] == {"full_name", "dob", "id_number", "address_permanent"}
    assert by_page[3] == {"account_holder", "account_number", "opening_balance", "closing_balance", "total"}
    assert "signature" in by_page[2]


def test_semantic_types_are_available_to_later_analyzers_but_not_in_the_output(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: X.id_card()})
    outcome = run_both(ctx)
    store = extraction_store(ctx)
    assert {store[f.fieldId].semanticType for f in outcome.fields} >= {"dob", "id_number", "person_name"}
    assert "semanticType" not in outcome.fields[0].model_dump()


def test_uses_the_o3_classification_and_skips_unsupported_types(settings, tmp_path):
    mixed = P.ocr_page(["Surname: SAMPLE", "Given names: JANE", "Date of birth: 12/08/1990",
                        "Nationality: EXAMPLELANDER", "ID number: 000-FICTIONAL-01", "Applicant: Jane Sample",
                        "Occupation: Clerk", "Employer: Sample Industries", "Marital status: Single", "Phone: 555-0142"])
    pages = [P.other_invoice(), P.blank_page(), mixed, P.random_text()]
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE] * 4)
    seed_ocr(ctx, {n: p for n, p in enumerate(pages, 1)})
    outcome = run_both(ctx)
    assert [classification_store(ctx)[n].docType for n in (1, 2, 3, 4)] == ["other", "UNKNOWN", "AMBIGUOUS", "UNKNOWN"]
    assert outcome.fields == []                                           # nothing was forced into a type
    assert outcome.statuses[NAME] == "NOT_APPLICABLE"
    cfg = prov(outcome).config
    assert cfg["page3.docType"] == "AMBIGUOUS" and "no extraction schema" in cfg["page3.skipped"]
    assert cfg["page1.docTypeSource"] == "classifier"


def test_classifier_decision_is_respected_even_if_the_page_looks_like_a_form(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE])
    seed_ocr(ctx, {1: X.application_form()})
    from dataclasses import replace
    from app.classify import classify_page
    from app.classifier_config import load_classifier_config
    forced = replace(classify_page(X.application_form(), load_classifier_config()), docType="other")
    classification_store(ctx)[1] = forced
    outcome = run_analyzers(ctx, registry(FieldExtractorAnalyzer), 30.0)
    assert outcome.fields == [] and outcome.statuses[NAME] == "NOT_APPLICABLE"


def test_without_the_classifier_the_type_is_decided_inline_and_safely(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [ID_SIZE, PAGE_SIZE])
    seed_ocr(ctx, {1: X.id_card(), 2: P.blank_page()})
    outcome = run_analyzers(ctx, registry(FieldExtractorAnalyzer), 30.0)
    cfg = prov(outcome).config
    assert cfg["page1.docTypeSource"] == "inline" and cfg["page1.docType"] == "id_card"
    assert cfg["page2.docType"] == "UNKNOWN" and len(outcome.fields) == 4
    assert classification_store(ctx) == {}                                # the extractor never writes classifications


def test_provenance_records_thresholds_schemas_missing_and_flags(settings, tmp_path):
    page = X.id_card(X.without_line(X.ID_LINES, "ID number"))
    for w in page.words:
        if w.text in ("12/08/1990",):
            w.ocrConfidence = 0.2
    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: page})
    outcome = run_both(ctx)
    cfg = prov(outcome).config
    st = extract_schema.load_settings()
    assert cfg["lowOcrConfidence"] == st.lowOcrConfidence and cfg["dateOrder"] == "DMY" and cfg["schema.id_card"] == "1"
    assert cfg["page1.missing"] == "id_number" and "dob(ocr=0.20)" in cfg["page1.lowConfidence"]
    assert any(f.name == "dob" for f in outcome.fields)                   # low confidence is flagged, never dropped
    assert all(isinstance(v, (int, float, str)) for v in cfg.values())


def test_missing_ocr_is_unavailable(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    ctx = make_ctx(settings, tmp_path, [PAGE_SIZE, PAGE_SIZE])
    outcome = run_analyzers(ctx, registry(FieldExtractorAnalyzer), 30.0)
    assert outcome.statuses == {NAME: "UNAVAILABLE"} and outcome.fields == []
    assert "OCR is not available" in prov(outcome).config["reason"]


def test_failed_ocr_page_is_an_error_status(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", lambda image, **kw: tsv_from_page(X.id_card(), *image.size))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    ctx = make_ctx(settings, tmp_path, [ID_SIZE, ID_SIZE], files=True)
    ctx.pages[1].path.write_bytes(b"corrupted")
    outcome = run_analyzers(ctx, registry(FieldExtractorAnalyzer), 30.0)
    assert outcome.statuses == {NAME: "ERROR"} and prov(outcome).config["pagesFailed"] == "2"


@pytest.fixture
def schema_dir(tmp_path, monkeypatch):
    target = tmp_path / "schemas"
    shutil.copytree(extract_schema.SCHEMAS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("FIELD_SCHEMAS_DIR", str(target))
    return target


def test_malformed_schema_is_an_error_and_the_classifier_output_survives(settings, tmp_path, schema_dir):
    (schema_dir / "id_card.yaml").write_text("fields: [", encoding="utf-8")
    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: X.id_card()})
    outcome = run_both(ctx)
    assert outcome.statuses == {"page.classifier": "AVAILABLE", NAME: "ERROR"}   # one failure, no crash
    assert "unreadable" in prov(outcome).config["reason"] and outcome.fields == []
    assert classification_store(ctx)[1].docType == "id_card"


def test_missing_schema_for_one_type_does_not_hide_another_types_error_or_crash(settings, tmp_path, schema_dir):
    (schema_dir / "bank_statement.yaml").unlink()
    ctx = make_ctx(settings, tmp_path, [ID_SIZE, PAGE_SIZE])
    seed_ocr(ctx, {1: X.id_card(), 2: X.bank_statement()})
    outcome = run_both(ctx)
    assert outcome.statuses[NAME] == "ERROR" and prov(outcome).config["pagesFailed"] == "2"
    assert "not found" in prov(outcome).config["reason"]


def test_missing_or_broken_settings_file_is_an_error(settings, tmp_path, schema_dir):
    (schema_dir / "_settings.yaml").write_text("scoring: {}\n", encoding="utf-8")
    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: X.id_card()})
    assert run_both(ctx).statuses[NAME] == "ERROR"


def test_a_crashing_analyzer_elsewhere_does_not_affect_extraction(settings, tmp_path):
    class Boom(Analyzer):
        name, version = "aaa.boom", "1"

        def run(self, ctx):
            raise RuntimeError("kaboom")

    ctx = make_ctx(settings, tmp_path, [ID_SIZE])
    seed_ocr(ctx, {1: X.id_card()})
    outcome = run_analyzers(ctx, registry(Boom, PageClassifierAnalyzer, FieldExtractorAnalyzer), 30.0)
    assert outcome.statuses == {"aaa.boom": "ERROR", "page.classifier": "AVAILABLE", NAME: "AVAILABLE"}
    assert len(outcome.fields) == 4


def test_ocr_is_not_recomputed(settings, tmp_path, monkeypatch):
    pages = [X.id_card(), X.application_form()]
    calls = []

    def image_to_data(image, **kw):
        calls.append(1)
        return tsv_from_page(pages[len(calls) - 1], *image.size)

    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    ctx = make_ctx(settings, tmp_path, [ID_SIZE, PAGE_SIZE], files=True)
    outcome = run_analyzers(ctx, registry(TesseractOcrAnalyzer, PageClassifierAnalyzer, FieldExtractorAnalyzer), 30.0)
    assert len(calls) == 2                                               # one OCR run per page for all three analyzers
    assert outcome.statuses == {"ocr": "AVAILABLE", "page.classifier": "AVAILABLE", NAME: "AVAILABLE"}
    assert {f.name for f in outcome.fields} >= {"dob", "id_number", "requested_amount", "signature"}


def test_response_with_fields_matches_the_contract(settings, tmp_path):
    from app.classify import apply_page_classifications
    ctx = make_ctx(settings, tmp_path, [ID_SIZE, PAGE_SIZE])
    seed_ocr(ctx, {1: X.id_card(), 2: X.bank_statement()})
    outcome = run_both(ctx)
    response = AnalyzeResponse(
        docId=ctx.doc_id, pipelineVersion="test", pages=apply_page_classifications(ctx, [p.record for p in ctx.pages]),
        fields=outcome.fields, signals=outcome.signals, analyzerStatuses=outcome.statuses, provenance=outcome.provenance)
    body = response.model_dump(mode="json", exclude_unset=True)
    AnalyzeResponse.model_validate(body)
    assert [p["docType"] for p in body["pages"]] == ["id_card", "bank_statement"]
    assert {f["name"] for f in body["fields"]} >= {"dob", "account_number"}
    assert all(set(f) == {"fieldId", "docId", "name", "rawValue", "normalizedValue", "ocrConfidence",
                          "extractionConfidence", "validationStatus", "region", "source"} for f in body["fields"])


# ---- through the real /analyze route (needs FastAPI + PyMuPDF installed)

def test_analyze_endpoint_returns_extracted_fields(make_client, put_original, monkeypatch):
    from tests.conftest import make_png_bytes
    page = X.id_card()
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", lambda image, **kw: tsv_from_page(page, *image.size))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    put_original("EX-API", "original.png", make_png_bytes((1011, 638)))
    client = make_client(registry(TesseractOcrAnalyzer, PageClassifierAnalyzer, FieldExtractorAnalyzer))
    r = client.post("/analyze", json={"docId": "EX-API"})
    assert r.status_code == 200
    body = r.json()
    AnalyzeResponse.model_validate(body)
    assert body["pages"][0]["docType"] == "id_card"
    by_name = {f["name"]: f for f in body["fields"]}
    assert by_name["dob"]["normalizedValue"] == "1990-08-12" and by_name["dob"]["region"]["page"] == 1
    assert body["analyzerStatuses"][NAME] == "AVAILABLE"
    assert next(p for p in body["provenance"] if p["analyzer"] == NAME)["config"]["lowOcrConfidence"] == 0.6


def test_analyze_endpoint_unknown_page_returns_no_fields(make_client, put_original, monkeypatch):
    from tests.conftest import make_png_bytes
    page = P.random_text()
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", lambda image, **kw: tsv_from_page(page, *image.size))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    put_original("EX-UNK", "original.png", make_png_bytes((1240, 1754)))
    client = make_client(registry(TesseractOcrAnalyzer, PageClassifierAnalyzer, FieldExtractorAnalyzer))
    body = client.post("/analyze", json={"docId": "EX-UNK"}).json()
    assert body["pages"][0]["docType"] == "UNKNOWN" and body["fields"] == []
    assert body["analyzerStatuses"][NAME] == "NOT_APPLICABLE"
