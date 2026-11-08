"""Recorded OCR fixtures (contracts/fixtures/ocr/<docId>-page-N.json).

The C1 fixtures are read from the shared contracts folder when it exists (those tests are skipped otherwise).
The loader itself is always tested with fixtures written to a temporary folder.
"""
import re

import pytest

from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.classifier_config import load_classifier_config
from app.classify import classification_store, classify_page
from app.models import PageRecord, RenderedDocument, RenderedPage
from app.context import AnalysisContext
from app.ocr_fixtures import fixtures_dir, list_fixtures, load_ocr_fixture, seed_ocr
from app.registry import AnalyzerRegistry, run_analyzers
from tests import classifier_pages as P

# C1 plan: D-01 id_card, D-02 application_form, D-03 bank_statement
EXPECTED_BY_DOC = {"D-01": "id_card", "D-02": "application_form", "D-03": "bank_statement"}
_DIR = fixtures_dir()
RECORDED = [(d, n, p) for d, n, p in (list_fixtures(_DIR) if _DIR else [])
            if any(d.endswith(k) for k in EXPECTED_BY_DOC)]


def _expected(doc_id):
    return next(v for k, v in EXPECTED_BY_DOC.items() if doc_id.endswith(k))


@pytest.mark.parametrize("doc_id,page,path", RECORDED, ids=[f"{d}-p{n}" for d, n, _ in RECORDED])
def test_recorded_c1_fixture_is_classified_correctly(doc_id, page, path):
    result = classify_page(load_ocr_fixture(path), load_classifier_config())
    assert result.docType == _expected(doc_id), (result.docType, result.scores, result.note)
    assert 0.0 <= result.confidence <= 1.0


def test_fixture_loader_round_trip_and_seeding(settings, tmp_path):
    for n, make in enumerate([P.id_card, P.application_form, P.bank_statement], 1):
        (tmp_path / f"D-0{n}-page-1.json").write_text(make(doc_id=f"D-0{n}").model_dump_json(), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    found = list_fixtures(tmp_path)
    assert [(d, n) for d, n, _ in found] == [("D-01", 1), ("D-02", 1), ("D-03", 1)]

    for doc_id, _, path in found:
        assert load_ocr_fixture(path).docId == doc_id
        ctx_pages = []
        record = PageRecord(docId=doc_id, page=1, renderPath=f"derived/{doc_id}/page-1.png", renderDpi=150.0,
                            rotationDeg=0.0, deskewDeg=0.0, docType="UNKNOWN", docTypeConfidence=0.0)
        ctx_pages.append(RenderedPage(record=record, path=tmp_path / "p.png", width_px=1240, height_px=1754))
        ctx = AnalysisContext(RenderedDocument(doc_id, tmp_path / "o.png", "0" * 64, ctx_pages), settings)
        seed_ocr(ctx, {1: load_ocr_fixture(path)})
        registry = AnalyzerRegistry()
        registry.register(PageClassifierAnalyzer)
        outcome = run_analyzers(ctx, registry, 30.0)
        assert outcome.statuses == {"page.classifier": "AVAILABLE"}
        assert classification_store(ctx)[1].docType == _expected(doc_id)


def test_fixtures_dir_is_none_when_the_folder_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_FIXTURES_DIR", str(tmp_path / "does-not-exist"))
    assert fixtures_dir() is None
    monkeypatch.setenv("OCR_FIXTURES_DIR", str(tmp_path))
    assert fixtures_dir() == tmp_path
