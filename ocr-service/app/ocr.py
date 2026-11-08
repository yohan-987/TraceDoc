"""OCR results in the shared analysis context.

Any analyzer that needs OCR calls ``get_page_ocr(ctx, page)``. The first call runs OCR and caches the
contract ``OcrPage`` in ``ctx`` (via ``ctx.get_ocr_page``); later calls, from any analyzer, return the
cached object. Failures are cached too, so a bad page is not retried by every analyzer.

Engine selection (``OCR_ENGINE``): ``auto`` (default) tries PaddleOCR first and falls back to Tesseract
per page when PaddleOCR is unavailable or fails; ``tesseract`` and ``paddleocr`` force one engine with no
fallback. ``OcrPage.engine`` always says which engine produced a page. PDF text-layer words are a
separate result (app/textlayer.py) and never replace OCR.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from . import ocr_paddle
from .context import AnalysisContext
from .models import OcrLine, OcrPage, OcrWord, RenderedPage
from .ocr_core import (
    ENGINE_NAME,
    EngineStatus,
    OcrPageError,
    OcrUnavailableError,
    engine_policy,
    ocr_image_file,
    page_timeout_s,
    resolve_engine,
)
from .ocr_paddle import PaddleFailure

logger = logging.getLogger(__name__)

__all__ = ["EngineStatus", "OcrPageError", "OcrUnavailableError", "get_engine", "get_page_ocr",
           "ocr_availability"]


def get_engine(ctx: AnalysisContext) -> EngineStatus:
    """Tesseract check, done once per request."""
    return ctx.get_or_compute(("ocr-engine",), resolve_engine)


def ocr_availability(ctx: AnalysisContext) -> Tuple[bool, Optional[str]]:
    """Can at least one allowed OCR engine run? (available, reason when not)."""
    policy = engine_policy()
    reasons = []
    if policy != "tesseract":
        _, paddle = ocr_paddle.get_paddle()
        if paddle.available:
            return True, None
        reasons.append(("PaddleOCR", paddle.reason))
    if policy != "paddleocr":
        tesseract = get_engine(ctx)
        if tesseract.available:
            return True, None
        reasons.append(("Tesseract", tesseract.reason))
    if len(reasons) == 1:
        return False, reasons[0][1]
    return False, "; ".join(f"{name}: {why}" for name, why in reasons)


def _to_ocr_page(ctx: AnalysisContext, rendered: RenderedPage, raw: Dict[str, Any],
                 engine: str, version: str) -> OcrPage:
    try:
        return OcrPage(
            docId=ctx.doc_id,
            page=rendered.page,
            widthPx=raw["widthPx"],
            heightPx=raw["heightPx"],
            renderDpi=rendered.record.renderDpi,
            engine=engine,
            engineVersion=version or "unknown",
            words=[OcrWord(**w) for w in raw["words"]],
            lines=[OcrLine(**line) for line in raw["lines"]],
        )
    except ValidationError as exc:
        raise OcrPageError("OCR output did not satisfy the OcrPage contract") from exc


def get_page_ocr(ctx: AnalysisContext, page: int, timeout_s: Optional[float] = None) -> OcrPage:
    """Return the OcrPage for a 1-based page number, running OCR only if nobody has yet.

    Raises OcrUnavailableError (no usable engine) or OcrPageError (this page failed).
    """
    failures = ctx.get_or_compute(("ocr-failures",), dict)
    if page in failures:
        raise OcrPageError(failures[page])
    paddle_state: Dict[str, str] = ctx.get_or_compute(("ocr-paddle-state",), dict)

    policy = engine_policy()
    paddle_status = None
    if policy != "tesseract" and "failed" not in paddle_state:
        _, paddle_status = ocr_paddle.get_paddle()
    use_paddle = paddle_status is not None and paddle_status.available
    tesseract = get_engine(ctx) if policy != "paddleocr" else None
    can_fall_back = tesseract is not None and tesseract.available

    if not use_paddle and not can_fall_back:
        if "failed" in paddle_state:
            raise OcrPageError(f"PaddleOCR failed: {paddle_state['failed']}; Tesseract is unavailable")
        _, reason = ocr_availability(ctx)
        raise OcrUnavailableError(reason or "no OCR engine available")

    rendered = next((p for p in ctx.pages if p.page == page), None)
    if rendered is None:
        raise OcrPageError(f"page {page} does not exist")
    limit = timeout_s if timeout_s is not None else page_timeout_s()

    def compute() -> OcrPage:
        if use_paddle:
            try:
                raw = ocr_paddle.ocr_image_file(rendered.path)
                return _to_ocr_page(ctx, rendered, raw, ocr_paddle.ENGINE_NAME, paddle_status.version or "")
            except PaddleFailure as exc:
                logger.warning("PaddleOCR failed on %s page %d (%s)", ctx.doc_id, page, exc)
                paddle_state["failed"] = str(exc)  # skip PaddleOCR for the rest of this request
                if not can_fall_back:
                    raise OcrPageError(f"PaddleOCR failed: {exc}") from exc
        raw = ocr_image_file(rendered.path, tesseract, rendered.record.renderDpi, limit)
        return _to_ocr_page(ctx, rendered, raw, ENGINE_NAME, tesseract.version or "")

    try:
        return ctx.get_ocr_page(page, compute)
    except OcrPageError as exc:
        failures[page] = str(exc)
        raise
