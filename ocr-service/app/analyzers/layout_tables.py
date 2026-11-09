"""Layout/table plugin (registered as "layout.tables"): bank-statement transaction rows and stated totals.

Reads the words of each page: the PDF text layer when the page has one (exact characters, so no digit can be misread;
the field's source says "pdf_text_layer"), otherwise the OCR already in the shared context (never runs OCR unless nobody
has). The text layer is the encoded text, not proof of what is visible; that comparison is not made here. Classifies the page like the field
extractor does, and for bank-statement pages reads the table from word geometry (app/layout.py). Emitted fields:
  txn_amount     one per CREDIT row   (the backend sums these against total_credits)
  debit_amount   one per DEBIT row    (summed against total_debits)
  total_credits / total_debits        the printed Totals row
Field ids are TBL-001.. (unique within the document, continuing across pages). The printed text is rawValue,
normalizedValue is a plain two-decimal string; a value that cannot be read is still returned (INVALID_FORMAT) so a
downstream sum is never silently short. Rows with problems are listed in provenance. Observations only: a balance
chain that does not add up is reported as a count in provenance, never as a conclusion.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from ..classifier_config import ClassifierConfigError, load_classifier_config
from ..classify import classification_store, classify_page
from ..context import AnalysisContext
from ..layout import AMOUNT_ROLES, Cell, Column, PageTable, balance_chain_breaks, parse_page
from ..layout_config import LayoutConfig, LayoutConfigError, load_layout_config
from ..models import AnalyzerResult, ExtractedField, OcrPage, Region
from ..ocr import OcrPageError, OcrUnavailableError, cached_page_ocr, get_page_ocr
from ..ocr_core import page_timeout_s
from ..registry import Analyzer, register_analyzer
from ..textlayer import SOURCE as TEXT_LAYER, get_text_layer

_BUDGET_FRACTION = 0.9
_ROW_FIELD = {"debit": "debit_amount", "credit": "txn_amount"}
_TOTAL_FIELD = {"debit": "total_debits", "credit": "total_credits"}


def _confidence(cfg: LayoutConfig, table: PageTable, cell: Cell) -> float:
    base = cfg.confidenceInheritedColumns if table.columns_inherited else cfg.confidenceHeaderOnPage
    if cell.ambiguous:
        base -= cfg.confidenceAmbiguityPenalty
    if cell.normalized is None:
        base = min(base, 0.3)
    return round(max(0.0, min(1.0, base)), 3)


@register_analyzer
class LayoutTablesAnalyzer(Analyzer):
    name = "layout.tables"
    version = "0.1"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        try:
            cfg = load_layout_config()
        except LayoutConfigError as exc:
            return AnalyzerResult(status="ERROR", reason=str(exc))
        classes = classification_store(ctx)
        deadline = time.monotonic() + ctx.settings.analyzer_timeout_s * _BUDGET_FRACTION
        fields: List[ExtractedField] = []
        unavailable: Dict[int, str] = {}
        problems: Dict[int, str] = {}
        notes: Dict[str, object] = {}
        inherited: Optional[Dict[str, Column]] = None
        statement_pages = tables_found = rows_total = unreadable = 0
        all_rows = []
        totals_seen = 0
        cls_cfg = None
        cls_loaded = False

        layer_pages = {}
        try:
            layer_pages = {n: tp.page for n, tp in get_text_layer(ctx).pages.items() if tp.page.words}
        except Exception:  # noqa: BLE001 - the text layer is an optimisation; OCR is the fallback
            layer_pages = {}

        def add(name: str, cell: Cell, page: int, table: PageTable, source: str = "ocr") -> None:
            valid = cell.normalized is not None
            fields.append(ExtractedField(
                fieldId=f"TBL-{len(fields) + 1:03d}", docId=ctx.doc_id, name=name, rawValue=cell.text,
                normalizedValue=cell.normalized, ocrConfidence=round(cell.ocr_confidence, 3),
                extractionConfidence=_confidence(cfg, table, cell),
                validationStatus="VALID" if valid else "INVALID_FORMAT",
                region=Region(docId=ctx.doc_id, page=page, bbox=tuple(round(v, 6) for v in cell.bbox), label=name),
                source=source))  # type: ignore[arg-type]

        for page in ctx.pages:
            classified = classes.get(page.page)
            layer_page = layer_pages.get(page.page)
            ocr_page: Optional[OcrPage] = layer_page
            source = TEXT_LAYER if layer_page is not None else "ocr"
            if ocr_page is None:
                ocr_page = cached_page_ocr(ctx, page.page)
            if ocr_page is None:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0.5:
                        raise OcrPageError("analyzer time budget exhausted")
                    ocr_page = get_page_ocr(ctx, page.page, timeout_s=min(page_timeout_s(), remaining))
                except OcrUnavailableError as exc:
                    unavailable[page.page] = str(exc)
                    continue
                except OcrPageError as exc:
                    problems[page.page] = str(exc)
                    continue
            if classified is not None:
                doc_type = classified.docType
            else:
                if not cls_loaded:
                    cls_loaded = True
                    try:
                        cls_cfg = load_classifier_config()
                    except ClassifierConfigError:
                        cls_cfg = None
                doc_type = classify_page(ocr_page, cls_cfg).docType if cls_cfg else "UNKNOWN"
            prefix = f"page{page.page}"
            notes[f"{prefix}.docType"] = doc_type
            notes[f"{prefix}.wordSource"] = source
            if doc_type != "bank_statement":
                notes[f"{prefix}.skipped"] = "not a bank statement page"
                continue
            statement_pages += 1
            try:
                table = parse_page(ocr_page, cfg, inherited)
            except Exception as exc:  # noqa: BLE001 - one page must not take the analyzer down
                problems[page.page] = f"table reading failed ({type(exc).__name__})"
                continue
            if table is None:
                notes[f"{prefix}.table"] = "none found"
                continue
            tables_found += 1
            inherited = table.columns
            notes[f"{prefix}.columns"] = ",".join(r for r in ("date", "description", "debit", "credit", "balance")
                                                  if r in table.columns)
            notes[f"{prefix}.columnsFrom"] = "previous page" if table.columns_inherited else "header on this page"
            notes[f"{prefix}.rows"] = len(table.rows)
            notes[f"{prefix}.skewDeg"] = round(table.skew_deg, 2)
            bad = []
            for i, row in enumerate(table.rows, 1):
                rows_total += 1
                all_rows.append(row)
                for role in ("debit", "credit"):
                    cell = row.cells.get(role)
                    if cell is not None:
                        add(_ROW_FIELD[role], cell, page.page, table, source)
                        if cell.normalized is None:
                            unreadable += 1
                            bad.append(f"row{i}:{role} unreadable")
                for msg in row.problems:
                    bad.append(f"row{i}:{msg}")
            if table.totals:
                totals_seen += 1
                for role in ("credit", "debit"):
                    cell = table.totals.get(role)
                    if cell is not None:
                        add(_TOTAL_FIELD[role], cell, page.page, table, source)
                        if cell.normalized is None:
                            bad.append(f"totals:{role} unreadable")
            bad.extend(f"totals:{m}" for m in table.totals_problems)
            if table.orphan_amounts:
                notes[f"{prefix}.amountsOutsideRows"] = table.orphan_amounts
            if bad:
                notes[f"{prefix}.rowProblems"] = "; ".join(bad)[:400]

        config: Dict[str, object] = {**cfg.as_provenance(), "pages": len(ctx.pages), "statementPages": statement_pages,
                                     "tablesFound": tables_found, "rows": rows_total, "unreadableAmounts": unreadable,
                                     "totalsRows": totals_seen, "fields": len(fields)}
        if all_rows:
            config["balanceChainBreaks"] = len(balance_chain_breaks(all_rows))
        if totals_seen > 1:
            config["totalsNote"] = "more than one totals row found; all emitted, a downstream check will see duplicates"
        config.update(notes)
        failed = {**unavailable, **problems}
        if unavailable and not problems and len(unavailable) == len(ctx.pages):
            return AnalyzerResult.unavailable(f"OCR is not available: {next(iter(unavailable.values()))}")
        if failed:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(failed))
            return AnalyzerResult(status="ERROR", reason="; ".join(f"page {n}: {m}" for n, m in sorted(failed.items())),
                                  model_version=f"layout-{cfg.configVersion}", config=config)
        if statement_pages == 0:
            return AnalyzerResult(status="NOT_APPLICABLE", reason="no page is classified as a bank statement",
                                  config=config)
        return AnalyzerResult(status="AVAILABLE", fields=fields, model_version=f"layout-{cfg.configVersion}",
                              config=config)
