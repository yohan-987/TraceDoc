"""Page classifier plugin (registered as "page.classifier").

Classifies every page independently from the OCR in the shared context (it never runs OCR a second time:
cached OCR, including recorded fixtures seeded into the context, is used first; live OCR through app/ocr.py is
the runtime source). Results go to the context (read by /analyze to fill PageRecord.docType and
docTypeConfidence) and the thresholds plus per-page evidence are echoed in provenance. It adds no fields or signals.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Dict, Optional

from ..classifier_config import ClassifierConfigError, load_classifier_config
from ..classifier_lexicon import LEXICON_VERSION
from ..classify import Evidence, PageClassification, classification_store, classify_page
from ..context import AnalysisContext
from ..models import AnalyzerResult, OcrPage
from ..ocr import OcrPageError, OcrUnavailableError, get_page_ocr
from ..ocr_core import page_timeout_s
from ..registry import Analyzer, register_analyzer

_BUDGET_FRACTION = 0.9
_MAX_EVIDENCE_CHARS = 400


class _NotCached(Exception):
    pass


def _raise_not_cached() -> OcrPage:
    raise _NotCached()


def _cached_ocr(ctx: AnalysisContext, page: int) -> Optional[OcrPage]:
    """OCR already in the context (live OCR from the ocr analyzer, or seeded recorded fixtures), else None."""
    try:
        return ctx.get_ocr_page(page, _raise_not_cached)  # raising leaves the cache untouched
    except _NotCached:
        return None


def _evidence_text(items: list[Evidence]) -> str:
    top = sorted(items, key=lambda e: -e.points)[:8]
    return "; ".join(e.short() for e in top)[:_MAX_EVIDENCE_CHARS]


def _page_config(result: PageClassification) -> Dict[str, object]:
    prefix = f"page{result.page}"
    out: Dict[str, object] = {
        f"{prefix}.docType": result.docType,
        f"{prefix}.confidence": result.confidence,
        f"{prefix}.scores": ",".join(f"{k}={v:.3f}" for k, v in result.scores.items()),
        f"{prefix}.evidence": _evidence_text(result.evidence.get(_winner(result), [])) or "none",
    }
    if result.note:
        out[f"{prefix}.note"] = result.note[:200]
    return out


def _winner(result: PageClassification) -> str:
    return max(result.scores, key=lambda c: result.scores[c])


@register_analyzer
class PageClassifierAnalyzer(Analyzer):
    name = "page.classifier"
    version = "0.1"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        try:
            cfg = load_classifier_config()
        except ClassifierConfigError as exc:
            return AnalyzerResult(status="ERROR", reason=str(exc))

        store = classification_store(ctx)
        deadline = time.monotonic() + ctx.settings.analyzer_timeout_s * _BUDGET_FRACTION
        failed: Dict[int, str] = {}
        unavailable: Dict[int, str] = {}
        page_config: Dict[str, object] = {}
        counts: Dict[str, int] = {}

        for page in ctx.pages:
            ocr_page = _cached_ocr(ctx, page.page)
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
                    failed[page.page] = str(exc)
                    continue
            # the context's page number is authoritative (a recorded fixture may carry a different one)
            result = replace(classify_page(ocr_page, cfg), page=page.page)
            store[page.page] = result
            counts[result.docType] = counts.get(result.docType, 0) + 1
            page_config.update(_page_config(result))

        config: Dict[str, object] = {**cfg.as_provenance(), "lexiconVersion": LEXICON_VERSION,
                                     "pages": len(ctx.pages), "pagesClassified": len(store)}
        config.update({f"count.{k}": v for k, v in sorted(counts.items())})
        config.update(page_config)
        model_version = f"rules-{LEXICON_VERSION}"

        if unavailable and not store:
            reason = next(iter(unavailable.values()))
            return AnalyzerResult.unavailable(f"OCR is not available: {reason}")
        problems = {**unavailable, **failed}
        if problems:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(problems))
            reason = "; ".join(f"page {n}: {msg}" for n, msg in sorted(problems.items()))
            return AnalyzerResult(status="ERROR", reason=reason, model_version=model_version, config=config)
        return AnalyzerResult(status="AVAILABLE", model_version=model_version, config=config)
