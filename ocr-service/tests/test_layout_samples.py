"""O5 against the repo's D1 statement samples with live Tesseract (skipped when samples/ or Tesseract are absent).

The manifest gives every transaction cell and the printed totals. Results are recorded, not tuned: a sample the
reader gets wrong is an xfail below with the reason, never a loosened rule.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.analyzers.layout_tables import LayoutTablesAnalyzer
from app.analyzers.ocr_tesseract import TesseractOcrAnalyzer
from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.context import AnalysisContext
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document
from tests.ocr_helpers import tesseract_available

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
MANIFEST = SAMPLES / "manifest.json"
pytestmark = [pytest.mark.skipif(not MANIFEST.exists(), reason="samples/ not present"),
              pytest.mark.skipif(not tesseract_available(), reason="Tesseract is not installed")]


def statements():
    docs = json.loads(MANIFEST.read_text())["documents"]
    return {d["doc_id"]: d for d in docs if d["doc_type"] == "bank_statement" and d.get("tables")}


def expected(doc):
    values = {f["name"]: f.get("value") for f in doc["fields"]}
    credits, debits = [], []
    for row in doc["tables"][0]["rows"]:
        if "credit" in row["cells"]:
            credits.append(values[row["cells"]["credit"]])
        if "debit" in row["cells"]:
            debits.append(values[row["cells"]["debit"]])
    return sorted(credits), sorted(debits), values["total_credits"], values["total_debits"]


def run(settings, put_original, doc):
    settings = type(settings)(**{**settings.__dict__, "render_dpi": 200})
    path = SAMPLES / doc["file"]
    doc_id = doc["doc_id"]
    put_original(doc_id, "original" + path.suffix, path.read_bytes())
    ctx = AnalysisContext(render_document(doc_id, settings), settings)
    registry = AnalyzerRegistry()
    for cls in (TesseractOcrAnalyzer, PageClassifierAnalyzer, LayoutTablesAnalyzer):
        registry.register(cls)
    outcome = run_analyzers(ctx, registry, 180.0)
    return outcome


def amounts(outcome, name):
    return sorted(f.normalizedValue for f in outcome.fields if f.name == name and f.normalizedValue is not None)


def fmt(values):
    return sorted(f"{Decimal(v):.2f}" for v in values)


KNOWN_MISSES = {
    "bank_statement_001_scan_hard": "Tesseract reads one debit as 4345.26 instead of 1345.26 (1 read as 4); the table is read "
                                    "correctly and balanceChainBreaks flags the row. Not tuned away.",
    "bank_statement_002_scan_hard": "Tesseract misreads 11.04 and 152.70 (as 41.04, 452.70) and loses the date of one row, "
                                    "so that row (61.56) is not read; balanceChainBreaks flags both. Not tuned away.",
}


@pytest.mark.parametrize("doc_id", sorted(statements()))
def test_rows_and_totals_match_the_manifest(settings, put_original, doc_id):
    if doc_id in KNOWN_MISSES:
        pytest.xfail(KNOWN_MISSES[doc_id])
    doc = statements()[doc_id]
    outcome = run(settings, put_original, doc)
    assert outcome.statuses["layout.tables"] == "AVAILABLE", outcome.provenance
    credits, debits, total_credits, total_debits = expected(doc)
    got_credits, got_debits = amounts(outcome, "txn_amount"), amounts(outcome, "debit_amount")
    assert got_credits == fmt(credits)
    assert got_debits == fmt(debits)
    assert amounts(outcome, "total_credits") == fmt([total_credits])
    assert amounts(outcome, "total_debits") == fmt([total_debits])
    # the rows add up to the printed totals on these genuine statements
    assert sum(Decimal(v) for v in got_credits) == Decimal(total_credits)
    assert sum(Decimal(v) for v in got_debits) == Decimal(total_debits)
    assert [f.fieldId for f in outcome.fields] == [f"TBL-{i:03d}" for i in range(1, len(outcome.fields) + 1)]
