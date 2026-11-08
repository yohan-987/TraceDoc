"""Tesseract OCR analyzer (registered as "ocr").

It produces no fields or signals: its output is the per-page OcrPage stored in the analysis context
(see app/ocr.py) for later analyzers. Its own response contribution is a status plus provenance.
"""
from __future__ import annotations

import time

from ..context import AnalysisContext
from ..models import AnalyzerResult
from ..ocr import OcrPageError, OcrUnavailableError, get_engine, get_page_ocr
from ..ocr_core import page_timeout_s
from ..registry import Analyzer, register_analyzer

_BUDGET_FRACTION = 0.9  # stop slightly before the registry's own timeout so no work is left running


@register_analyzer
class TesseractOcrAnalyzer(Analyzer):
    name = "ocr"
    version = "0.1"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        engine = get_engine(ctx)
        if not engine.available:
            return AnalyzerResult.unavailable(engine.reason or "Tesseract unavailable")

        deadline = time.monotonic() + ctx.settings.analyzer_timeout_s * _BUDGET_FRACTION
        failed: dict[int, str] = {}
        word_count = line_count = ok_pages = 0
        for page in ctx.pages:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0.5:
                    raise OcrPageError("analyzer time budget exhausted")
                result = get_page_ocr(ctx, page.page, timeout_s=min(page_timeout_s(), remaining))
            except OcrUnavailableError as exc:
                return AnalyzerResult.unavailable(str(exc))
            except OcrPageError as exc:
                failed[page.page] = str(exc)
                continue
            ok_pages += 1
            word_count += len(result.words)
            line_count += len(result.lines)

        config = {"lang": engine.lang, "pages": len(ctx.pages), "pagesOk": ok_pages,
                  "words": word_count, "lines": line_count}
        model_version = f"tesseract {engine.version}" if engine.version else None
        if failed:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(failed))
            reason = "; ".join(f"page {n}: {msg}" for n, msg in sorted(failed.items()))
            return AnalyzerResult(status="ERROR", reason=reason, model_version=model_version, config=config)
        reason = "no text detected on any page" if word_count == 0 else None
        return AnalyzerResult(status="AVAILABLE", reason=reason, model_version=model_version, config=config)
