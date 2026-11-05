"""Canonical rendering: every page becomes one upright PNG at DATA_DIR/derived/<docId>/page-N.png.

All later normalized boxes refer to these renders. PyMuPDF (AGPL-3.0) renders PDFs; Pillow loads
images. Images are NOT resampled (native pixels are kept for later forensics); ``renderDpi`` for an
image is the nominal configured DPI.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
from pathlib import Path
from typing import List

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .models import (
    PageRecord,
    RenderedDocument,
    RenderedPage,
    validate_doc_id,
)

logger = logging.getLogger(__name__)

_EXIF_ORIENTATION = 0x0112
# Clockwise degrees applied to the original to make it upright. Mirroring (orientations 2, 4, 5, 7)
# is corrected in the pixels but not recorded: the contract has no mirror field.
_EXIF_ROTATION = {1: 0.0, 2: 0.0, 3: 180.0, 4: 180.0, 5: 270.0, 6: 90.0, 7: 90.0, 8: 270.0}
_STALE_PAGE_RE = re.compile(r"page-(\d+)\.png")


class RenderError(Exception):
    """Base class for rendering problems."""


class DocumentNotFoundError(RenderError):
    pass


class AmbiguousOriginalError(RenderError):
    pass


class UnsupportedDocumentError(RenderError):
    pass


def find_original(doc_id: str, settings: Settings) -> Path:
    """Locate DATA_DIR/evidence/<docId>/original.<ext>. The only input is the validated docId."""
    validate_doc_id(doc_id)
    root = settings.evidence_dir.resolve()
    doc_dir = root / doc_id
    if doc_dir.is_symlink() or not doc_dir.is_dir() or doc_dir.resolve().parent != root:
        raise DocumentNotFoundError("document not found")
    candidates = [
        p for p in doc_dir.iterdir()
        if p.is_file() and not p.is_symlink() and p.stem.lower() == "original"
    ]
    if not candidates:
        raise DocumentNotFoundError("document not found")
    if len(candidates) > 1:
        raise AmbiguousOriginalError("more than one original.* file for this document")
    return candidates[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"%PDF-" in fh.read(1024)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _make_page(doc_id: str, n: int, png: bytes, width: int, height: int, rotation: float,
               settings: Settings) -> RenderedPage:
    out_dir = settings.derived_dir / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"page-{n}.png"
    _write_atomic(out_path, png)
    record = PageRecord(
        docId=doc_id,
        page=n,
        renderPath=f"derived/{doc_id}/page-{n}.png",  # relative to DATA_DIR
        renderDpi=float(settings.render_dpi),
        rotationDeg=rotation,
        deskewDeg=0.0,  # no deskew estimation in O1
        docType="UNKNOWN",  # classification is a later batch
        docTypeConfidence=0.0,
    )
    return RenderedPage(record=record, path=out_path, width_px=width, height_px=height)


def _render_pdf(doc_id: str, path: Path, settings: Settings) -> List[RenderedPage]:
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as exc:
        raise UnsupportedDocumentError("could not open PDF") from exc
    pages: List[RenderedPage] = []
    with doc:
        if doc.needs_pass:
            raise UnsupportedDocumentError("PDF is encrypted")
        if doc.page_count == 0:
            raise UnsupportedDocumentError("PDF has no pages")
        if doc.page_count > settings.max_pages:
            raise UnsupportedDocumentError(f"PDF has more than {settings.max_pages} pages")
        try:
            for index in range(doc.page_count):
                page = doc.load_page(index)
                pix = page.get_pixmap(dpi=settings.render_dpi, alpha=False)
                # PyMuPDF applies the page /Rotate flag, so the render is already upright.
                pages.append(_make_page(doc_id, index + 1, pix.tobytes("png"), pix.width,
                                        pix.height, float(page.rotation), settings))
        except RenderError:
            raise
        except Exception as exc:
            raise UnsupportedDocumentError(f"failed to render PDF page {len(pages) + 1}") from exc
    return pages


def _render_image(doc_id: str, path: Path, settings: Settings) -> List[RenderedPage]:
    try:
        with Image.open(path) as img:
            img.load()
            orientation = int(img.getexif().get(_EXIF_ORIENTATION, 1) or 1)
            upright = _to_rgb(ImageOps.exif_transpose(img))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UnsupportedDocumentError("unsupported or corrupt image") from exc
    buf = io.BytesIO()
    upright.save(buf, format="PNG")
    page = _make_page(doc_id, 1, buf.getvalue(), upright.width, upright.height,
                      _EXIF_ROTATION.get(orientation, 0.0), settings)
    return [page]


def _remove_stale_pages(doc_id: str, keep: int, settings: Settings) -> None:
    out_dir = settings.derived_dir / doc_id
    for p in out_dir.glob("page-*.png"):
        m = _STALE_PAGE_RE.fullmatch(p.name)
        if m and int(m.group(1)) > keep:
            p.unlink(missing_ok=True)


def render_document(doc_id: str, settings: Settings) -> RenderedDocument:
    """Render every page of ``docId`` to the canonical PNGs (1-based page numbers)."""
    source = find_original(doc_id, settings)
    pages = (_render_pdf if _is_pdf(source) else _render_image)(doc_id, source, settings)
    _remove_stale_pages(doc_id, len(pages), settings)
    logger.info("rendered %s: %d page(s) at %d dpi", doc_id, len(pages), settings.render_dpi)
    return RenderedDocument(doc_id=doc_id, source_path=source, input_sha256=_sha256(source), pages=pages)
