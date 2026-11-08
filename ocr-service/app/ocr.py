"""OCR results in the shared analysis context (O2).

Any analyzer that needs OCR calls ``get_page_ocr(ctx, page)``. The first call runs Tesseract and caches
the contract ``OcrPage`` in ``ctx`` (via ``ctx.get_ocr_page``); later calls, from any analyzer, return
the cached object. Failures are cached too, so a bad page is not retried by every analyzer.
"""
from __future__ import annotations

from typing import Optional

from pydantic import ValidationError

from .context import AnalysisContext
from .models import OcrLine, OcrPage, OcrWord
from .ocr_core import (
    ENGINE_NAME,
    EngineStatus,
    OcrPageError,
    OcrUnavailableError,
    ocr_image_file,
    page_timeout_s,
    resolve_engine,
)

__all__ = ["EngineStatus", "OcrPageError", "OcrUnavailableError", "get_engine", "get_page_ocr"]


def get_engine(ctx: AnalysisContext) -> EngineStatus:
    """Engine check, done once per request."""
    return ctx.get_or_compute(("ocr-engine",), resolve_engine)


def get_page_ocr(ctx: AnalysisContext, page: int, timeout_s: Optional[float] = None) -> OcrPage:
    """Return the OcrPage for a 1-based page number, running OCR only if nobody has yet.

    Raises OcrUnavailableError (no usable Tesseract) or OcrPageError (this page failed).
    """
    failures = ctx.get_or_compute(("ocr-failures",), dict)
    if page in failures:
        raise OcrPageError(failures[page])
    engine = get_engine(ctx)
    if not engine.available:
        raise OcrUnavailableError(engine.reason or "Tesseract unavailable")
    rendered = next((p for p in ctx.pages if p.page == page), None)
    if rendered is None:
        raise OcrPageError(f"page {page} does not exist")
    limit = timeout_s if timeout_s is not None else page_timeout_s()

    def compute() -> OcrPage:
        raw = ocr_image_file(rendered.path, engine, rendered.record.renderDpi, limit)
        try:
            return OcrPage(
                docId=ctx.doc_id,
                page=page,
                widthPx=raw["widthPx"],
                heightPx=raw["heightPx"],
                renderDpi=rendered.record.renderDpi,
                engine=ENGINE_NAME,
                engineVersion=engine.version or "unknown",
                words=[OcrWord(**w) for w in raw["words"]],
                lines=[OcrLine(**line) for line in raw["lines"]],
            )
        except ValidationError as exc:
            raise OcrPageError("OCR output did not satisfy the OcrPage contract") from exc

    try:
        return ctx.get_ocr_page(page, compute)
    except OcrPageError as exc:
        failures[page] = str(exc)
        raise
