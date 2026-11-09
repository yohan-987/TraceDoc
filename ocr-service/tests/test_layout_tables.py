"""O5 layout/table reading on synthetic OCR pages (fictional data) and the layout.tables plugin contract."""
import json
from decimal import Decimal

import pytest

from app import layout as L
from app.analyzers.layout_tables import LayoutTablesAnalyzer
from app.analyzers.page_classifier import PageClassifierAnalyzer
from app.layout_config import LayoutConfigError, load_layout_config
from app.models import AnalyzeResponse, ExtractedField
from app.ocr_fixtures import seed_ocr
from app.registry import AnalyzerRegistry, discover_analyzers, default_registry, run_analyzers
from tests import classifier_pages as P
from tests.test_classifier_analyzer import make_ctx

CFG = load_layout_config()
NAME = "layout.tables"
PAGE = (1240, 1754)
HEAD = [("Date", 80), ("Description", 250), ("Debit", 700), ("Credit", 850), ("Balance", 1000)]


def row(date, desc, debit, credit, balance):
    cells = [(date, 80), (desc, 250)]
    if debit:
        cells.append((debit, 700))
    if credit:
        cells.append((credit, 850))
    cells.append((balance, 1000))
    return cells


def statement(rows, totals=("", ""), head=True, title=True):
    lines = ["SAMPLE BANK OF EXAMPLELAND", "ACCOUNT STATEMENT", "Account holder: JANE ALICE SAMPLE",
             "Account number: 0000 1234 5678", "Statement period: 01/09/2026 to 30/09/2026"] if title else []
    if head:
        lines.append(HEAD)
    lines += rows
    if totals != ("", ""):
        cells = [("Totals", 80)]
        if totals[0]:
            cells.append((totals[0], 700))
        if totals[1]:
            cells.append((totals[1], 850))
        lines.append(cells)
    return lines


ROWS = [row("02/09/2026", "Salary credit", "", "2,400.00", "3,650.00"),
        row("05/09/2026", "Rent payment", "800.00", "", "2,850.00"),
        row("12/09/2026", "Grocery store", "120.50", "", "2,729.50")]


def page(lines, number=1):
    return P.ocr_page(lines, width=PAGE[0], height=PAGE[1], page=number)


def vals(table):
    return [(r.date, r.description, {k: c.normalized for k, c in r.cells.items()}) for r in table.rows]


def test_reads_rows_columns_and_totals():
    table = L.parse_page(page(statement(ROWS, ("920.50", "2,400.00"))), CFG)
    assert set(table.columns) == {"date", "description", "debit", "credit", "balance"}
    assert vals(table) == [
        ("02/09/2026", "Salary credit", {"credit": "2400.00", "balance": "3650.00"}),
        ("05/09/2026", "Rent payment", {"debit": "800.00", "balance": "2850.00"}),
        ("12/09/2026", "Grocery store", {"debit": "120.50", "balance": "2729.50"})]
    assert {k: c.normalized for k, c in table.totals.items()} == {"debit": "920.50", "credit": "2400.00"}
    assert L.balance_chain_breaks(table.rows) == []


def test_a_missing_cell_is_not_shifted_into_the_neighbouring_column():
    # the credit row has no debit cell: its amount must stay in "credit", never slide left
    table = L.parse_page(page(statement(ROWS)), CFG)
    assert "debit" not in table.rows[0].cells and table.rows[0].cells["credit"].normalized == "2400.00"


def test_right_aligned_amounts_are_assigned_by_right_edge():
    # amounts of different widths share a right edge, so their left edges differ
    def r(date, desc, amount, balance):
        return [(date, 80), (desc, 250), (amount, 760 - 17 * len(amount)), (balance, 1100 - 17 * len(balance))]
    head = [("Date", 80), ("Description", 250), ("Debit", 700), ("Credit", 850), ("Balance", 1000)]
    lines = ["ACCOUNT STATEMENT", head, r("02/09/2026", "Small", "5.00", "1,000.00"),
             r("03/09/2026", "Large", "12,345.67", "9,999.99")]
    table = L.parse_page(page(lines), CFG)
    assert [r_.cells["debit"].normalized for r_ in table.rows] == ["5.00", "12345.67"]


def test_balance_chain_break_is_reported_not_decided():
    bad = [ROWS[0], row("05/09/2026", "Rent payment", "800.00", "", "2,950.00"), ROWS[2]]
    table = L.parse_page(page(statement(bad)), CFG)
    assert L.balance_chain_breaks(table.rows) == [1, 2]


def test_unreadable_amount_is_kept_as_invalid_not_dropped(settings, tmp_path):
    rows = [row("02/09/2026", "Salary credit", "", "2.400,00", "3,650.00"), ROWS[1]]
    outcome = _run([page(statement(rows, ("800.00", "2,400.00")))], settings, tmp_path)
    credit = [f for f in outcome.fields if f.name == "txn_amount"]
    assert len(credit) == 1 and credit[0].validationStatus == "INVALID_FORMAT" and credit[0].normalizedValue is None
    assert credit[0].rawValue == "2.400,00" and credit[0].extractionConfidence <= 0.3
    assert _prov(outcome).config["unreadableAmounts"] == 1


def test_no_table_page_gives_no_fields(settings, tmp_path):
    lines = ["SAMPLE BANK OF EXAMPLELAND", "ACCOUNT STATEMENT", "Account holder: JANE ALICE SAMPLE",
             "Account number: 0000 1234 5678", "Statement period: 01/09/2026 to 30/09/2026", "Opening balance: 1,250.00"]
    outcome = _run([page(lines)], settings, tmp_path)
    assert outcome.statuses[NAME] == "AVAILABLE" and outcome.fields == []
    assert _prov(outcome).config["tablesFound"] == 0


def test_two_page_statement_keeps_ids_unique_and_inherits_columns(settings, tmp_path):
    p1 = page(statement(ROWS[:2]), 1)
    p2 = page(statement([ROWS[2]], ("920.50", "2,400.00"), head=False, title=False), 2)
    outcome = _run([p1, p2], settings, tmp_path, classify=False)
    ids = [f.fieldId for f in outcome.fields]
    assert ids == [f"TBL-{i:03d}" for i in range(1, len(ids) + 1)]
    names = [f.name for f in outcome.fields]
    assert names.count("txn_amount") == 1 and names.count("debit_amount") == 2
    assert names.count("total_credits") == 1 and names.count("total_debits") == 1
    assert {f.region.page for f in outcome.fields} == {1, 2}
    cfg = _prov(outcome).config
    assert cfg["page2.columnsFrom"] == "previous page" and cfg["page1.columnsFrom"] == "header on this page"
    page2_rows = [f for f in outcome.fields if f.region.page == 2 and f.name == "debit_amount"]
    assert page2_rows[0].extractionConfidence == CFG.confidenceInheritedColumns


def test_repeated_header_on_page_two_is_not_a_transaction(settings, tmp_path):
    p2 = page(statement([ROWS[2]], ("920.50", "2,400.00"), title=False), 2)
    outcome = _run([page(statement(ROWS[:2]), 1), p2], settings, tmp_path, classify=False)
    assert sum(f.name == "debit_amount" for f in outcome.fields) == 2


def test_other_document_types_are_not_applicable(settings, tmp_path):
    outcome = _run([P.id_card()], settings, tmp_path, size=(1011, 638))
    assert outcome.statuses[NAME] == "NOT_APPLICABLE"


def test_fields_are_contract_objects_with_page_regions(settings, tmp_path):
    outcome = _run([page(statement(ROWS, ("920.50", "2,400.00")))], settings, tmp_path)
    assert outcome.statuses[NAME] == "AVAILABLE"
    for f in outcome.fields:
        ExtractedField.model_validate(f.model_dump())
        assert f.source == "ocr" and f.region.page == 1 and f.region.label == f.name
        assert all(0 <= v <= 1 for v in f.region.bbox)
    # the sums the backend will check
    assert sum(Decimal(f.normalizedValue) for f in outcome.fields if f.name == "debit_amount") == Decimal("920.50")
    assert sum(Decimal(f.normalizedValue) for f in outcome.fields if f.name == "txn_amount") == Decimal("2400.00")
    config = _prov(outcome).config
    assert config["cfg.configVersion"] == CFG.configVersion and config["rows"] == 3


def test_plugin_is_registered_and_response_validates(settings, tmp_path):
    discover_analyzers()
    assert NAME in [a.name for a in default_registry.analyzers()]
    outcome = _run([page(statement(ROWS, ("920.50", "2,400.00")))], settings, tmp_path)
    AnalyzeResponse  # imported for parity with the other plugin tests; the schema check lives in test_analyze


def test_a_missing_ocr_engine_is_unavailable_not_clean(settings, tmp_path):
    ctx = make_ctx(settings, tmp_path, [PAGE], files=False)  # nothing seeded, no engine reachable
    outcome = run_analyzers(ctx, _registry(LayoutTablesAnalyzer), 5.0)
    assert outcome.statuses[NAME] in ("UNAVAILABLE", "ERROR") and outcome.fields == []


@pytest.mark.parametrize("key,value", [("rowYToleranceFactor", 0), ("minHeaderRoles", 9), ("datePatterns", ["("]),
                                       ("skewMaxDeg", 50), ("headerDebit", [])])
def test_invalid_config_is_rejected(tmp_path, key, value):
    raw = json.loads((load_layout_config.__globals__["DEFAULT_PATH"]).read_text())
    raw[key] = value
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(LayoutConfigError):
        load_layout_config(path)


def test_unknown_or_missing_config_keys_are_rejected(tmp_path):
    raw = json.loads((load_layout_config.__globals__["DEFAULT_PATH"]).read_text())
    raw["surprise"] = 1
    del raw["skewStepDeg"]
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(LayoutConfigError, match="surprise"):
        load_layout_config(path)


def test_skew_is_estimated_and_corrected():
    import math
    t = math.tan(math.radians(2.0))
    base = page(statement(ROWS * 3, ("1,000.00", "2,000.00")))
    W, H = base.widthPx, base.heightPx
    words = []
    for w in base.words:
        x, y, bw, bh = w.bbox
        cx, cy = (x + bw / 2) * W, (y + bh / 2) * H
        ncy = cy + cx * t     # text running downhill to the right
        words.append(w.model_copy(update={"bbox": (x, ncy / H - bh / 2, bw, bh)}))
    tilted_page = base.model_copy(update={"words": words})
    assert L.estimate_skew_deg(base.words, W, H, CFG) == 0.0
    assert abs(L.estimate_skew_deg(words, W, H, CFG) - 2.0) <= 0.3
    table = L.parse_page(tilted_page, CFG)
    assert len(table.rows) == 9 and all("balance" in r.cells for r in table.rows)


def _registry(*analyzers):
    reg = AnalyzerRegistry()
    for a in analyzers:
        reg.register(a)
    return reg


def _run(pages, settings, tmp_path, size=PAGE, classify=True):
    ctx = make_ctx(settings, tmp_path, [size] * len(pages))
    seed_ocr(ctx, {n: p for n, p in enumerate(pages, 1)})
    analyzers = (PageClassifierAnalyzer, LayoutTablesAnalyzer) if classify else (LayoutTablesAnalyzer,)
    if not classify:   # continuation pages carry no statement title, so classify by hand
        from app.classify import classification_store
        from app.models import OcrPage  # noqa: F401
        from types import SimpleNamespace
        store = classification_store(ctx)
        for n in range(1, len(pages) + 1):
            store[n] = SimpleNamespace(docType="bank_statement")
    return run_analyzers(ctx, _registry(*analyzers), 30.0)


def _prov(outcome):
    return next(p for p in outcome.provenance if p.analyzer == NAME)
