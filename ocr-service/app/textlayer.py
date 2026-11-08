"""Digital-PDF text-layer words (source ``pdf_text_layer``), extracted with PyMuPDF (AGPL, see README).

The contract's OcrWord/OcrPage have no ``source`` field and cannot be extended, so the source travels on an
internal ``TextLayerPage`` wrapper. Text-layer words live in their own ctx entry and never replace or merge
into OCR results; a consumer asks for each explicitly and compares.

Text-layer words have no recognition confidence. The contract requires ``ocrConfidence``, so it is set to
1.0, meaning only "exactly the text encoded in the PDF". It does NOT mean the visible page shows that text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pymupdf

from .context import AnalysisContext
from .models import OcrLine, OcrPage, OcrWord
from .ocr_core import assemble_lines, normalize_box
from .render import looks_like_pdf

SOURCE = "pdf_text_layer"
ENGINE = "pymupdf-text-layer"

RectFn = Callable[[float, float, float, float], Tuple[float, float, float, float]]


class TextLayerUnavailableError(Exception):
    """No text-layer result for this document/page (not a PDF, unreadable, or page failed)."""


@dataclass(frozen=True)
class TextLayerPage:
    source: str   # always "pdf_text_layer"
    page: OcrPage  # same contract shape as OCR output; page.engine is "pymupdf-text-layer"


@dataclass(frozen=True)
class TextLayerResult:
    pages: Dict[int, TextLayerPage] = field(default_factory=dict)
    errors: Dict[int, str] = field(default_factory=dict)
    not_pdf: bool = False
    error: Optional[str] = None  # whole-document failure


def is_pdf(path: Path) -> bool:
    return looks_like_pdf(path)  # the same test the renderer used, so the two can never disagree


def words_to_page_dict(raw_words: List[Any], width_px: int, height_px: int, scale_x: float, scale_y: float,
                       to_page_rect: Optional[RectFn] = None) -> Dict[str, Any]:
    """PyMuPDF ``get_text("words")`` tuples -> word/line dicts normalized against the canonical render.

    ``to_page_rect`` maps a word rect into the displayed (rotated) page's point space; ``scale_*`` convert
    points to canonical pixels (width_px / page width in points).
    """
    groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for item in raw_words:
        x0, y0, x1, y1, text, block, line = item[:7]
        text = str(text).strip()
        if not text:
            continue
        rx0, ry0, rx1, ry1 = to_page_rect(x0, y0, x1, y1) if to_page_rect else (x0, y0, x1, y1)
        box = normalize_box(rx0 * scale_x, ry0 * scale_y, (rx1 - rx0) * scale_x, (ry1 - ry0) * scale_y,
                            width_px, height_px)
        if box is None:
            continue
        groups.setdefault((int(block), int(line)), []).append({"text": text, "bbox": box, "ocrConfidence": 1.0})
    words, lines = assemble_lines(groups)
    return {"widthPx": width_px, "heightPx": height_px, "words": words, "lines": lines}


def _extract_all(ctx: AnalysisContext) -> TextLayerResult:
    path = ctx.document.source_path
    if not is_pdf(path):
        return TextLayerResult(not_pdf=True, error="original is not a PDF")
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception:  # noqa: BLE001
        return TextLayerResult(error="could not open PDF")
    pages: Dict[int, TextLayerPage] = {}
    errors: Dict[int, str] = {}
    version = str(getattr(pymupdf, "VersionBind", "unknown"))
    with doc:
        for rendered in ctx.pages:
            try:
                pdf_page = doc.load_page(rendered.page - 1)
                matrix = pdf_page.rotation_matrix  # words come in unrotated page space; the render is rotated

                def to_rect(x0: float, y0: float, x1: float, y1: float, m=matrix) -> Tuple[float, float, float, float]:
                    r = pymupdf.Rect(x0, y0, x1, y1).transform(m)
                    return (r.x0, r.y0, r.x1, r.y1)

                rect = pdf_page.rect
                raw = words_to_page_dict(
                    pdf_page.get_text("words"), rendered.width_px, rendered.height_px,
                    rendered.width_px / rect.width, rendered.height_px / rect.height, to_rect)
                page = OcrPage(
                    docId=ctx.doc_id, page=rendered.page, widthPx=raw["widthPx"], heightPx=raw["heightPx"],
                    renderDpi=rendered.record.renderDpi, engine=ENGINE, engineVersion=version,
                    words=[OcrWord(**w) for w in raw["words"]], lines=[OcrLine(**ln) for ln in raw["lines"]])
                pages[rendered.page] = TextLayerPage(source=SOURCE, page=page)
            except Exception as exc:  # noqa: BLE001 - one bad page must not hide the rest
                errors[rendered.page] = f"{type(exc).__name__}"
    return TextLayerResult(pages=pages, errors=errors)


def get_text_layer(ctx: AnalysisContext) -> TextLayerResult:
    """Whole-document extraction, computed once per request and cached in ctx."""
    return ctx.get_or_compute(("text-layer",), lambda: _extract_all(ctx))


def get_text_layer_page(ctx: AnalysisContext, page: int) -> TextLayerPage:
    """Text-layer words of a 1-based page. Raises TextLayerUnavailableError if there is no result."""
    result = get_text_layer(ctx)
    if result.error:
        raise TextLayerUnavailableError(result.error)
    if page in result.errors:
        raise TextLayerUnavailableError(f"text-layer extraction failed for page {page}")
    if page not in result.pages:
        raise TextLayerUnavailableError(f"page {page} does not exist")
    return result.pages[page]
