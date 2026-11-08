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
import tempfile
from pathlib import Path
from typing import List, Tuple

import pymupdf
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

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


class RenderStorageError(RenderError):
    """The render could not be written (disk full, permissions): a server problem, not a bad document."""


def find_original(doc_id: str, settings: Settings) -> Path:
    """Locate DATA_DIR/evidence/<docId>/original.<ext>. The only input is the validated docId."""
    validate_doc_id(doc_id)
    root = settings.evidence_dir.resolve()
    doc_dir = root / doc_id
    try:
        if doc_dir.is_symlink() or not doc_dir.is_dir() or doc_dir.resolve().parent != root:
            raise DocumentNotFoundError("document not found")
        candidates = [
            p for p in doc_dir.iterdir()
            if p.is_file() and not p.is_symlink() and p.stem.lower() == "original"
        ]
    except OSError as exc:  # e.g. ENAMETOOLONG, EACCES: the document cannot be located
        raise DocumentNotFoundError("document not found") from exc
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


_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+",
                b"BM", b"RIFF")


def looks_like_pdf(path: Path) -> bool:
    """A PDF has '%PDF-' within its first 1024 bytes (the spec allows leading junk). A file that starts with a known\n    image signature is an image even if '%PDF-' appears inside its metadata."""
    with path.open("rb") as fh:
        head = fh.read(1024)
    if head.startswith(_IMAGE_MAGIC):
        return False
    return b"%PDF-" in head


_is_pdf = looks_like_pdf  # historical name


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a unique temp file in the same folder, then rename. Unique, so two concurrent renders of the
    same document cannot trip over each other's temp file (the later rename simply wins)."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


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
    out_path = out_dir / f"page-{n}.png"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(out_path, png)
    except OSError as exc:
        raise RenderStorageError("could not store the rendered page") from exc
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


def _check_pixels(width: float, height: float, settings: Settings, what: str) -> None:
    """Refuse inputs whose decoded size would exhaust memory (e.g. a tiny file that expands to hundreds of megapixels)."""
    if width * height > settings.max_image_pixels:
        raise UnsupportedDocumentError(f"{what} is too large to render (limit {settings.max_image_pixels} pixels)")


class _PixelBudget:
    """Total pixels rendered for one document, so many near-limit pages cannot stall the service or fill the disk."""

    def __init__(self, settings: Settings) -> None:
        self.limit, self.used = settings.max_total_pixels, 0

    def spend(self, pixels: float, what: str) -> None:
        self.used += pixels
        if self.used > self.limit:
            raise UnsupportedDocumentError(f"{what}: the document is too large to render (limit {self.limit} pixels in total)")


def _render_pdf(doc_id: str, path: Path, settings: Settings) -> List[RenderedPage]:
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as exc:
        raise UnsupportedDocumentError("could not open PDF") from exc
    pages: List[RenderedPage] = []
    budget = _PixelBudget(settings)
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
                scale = settings.render_dpi / 72.0
                pixels = page.rect.width * scale * page.rect.height * scale
                _check_pixels(page.rect.width * scale, page.rect.height * scale, settings, f"PDF page {index + 1}")
                budget.spend(pixels, f"PDF page {index + 1}")
                pix = page.get_pixmap(dpi=settings.render_dpi, alpha=False)
                # PyMuPDF applies the page /Rotate flag, so the render is already upright.
                pages.append(_make_page(doc_id, index + 1, pix.tobytes("png"), pix.width,
                                        pix.height, float(page.rotation), settings))
        except RenderError:
            raise
        except Exception as exc:  # PyMuPDF could not render this page: a problem with the document
            raise UnsupportedDocumentError(f"failed to render PDF page {len(pages) + 1}") from exc
    return pages


def _orientation(frame: Image.Image) -> int:
    """EXIF orientation 1..8; anything missing or malformed counts as 1 (upright)."""
    try:
        value = int(frame.getexif().get(_EXIF_ORIENTATION, 1) or 1)
    except (TypeError, ValueError, OverflowError):
        return 1
    return value if value in _EXIF_ROTATION else 1


def _frame_to_png(frame: Image.Image) -> Tuple[bytes, int, int, float]:
    orientation = _orientation(frame)
    upright = _to_rgb(ImageOps.exif_transpose(frame))
    buf = io.BytesIO()
    upright.save(buf, format="PNG")
    return buf.getvalue(), upright.width, upright.height, _EXIF_ROTATION.get(orientation, 0.0)


def _render_image(doc_id: str, path: Path, settings: Settings) -> List[RenderedPage]:
    """One page per image; a multi-page TIFF yields one page per frame (nothing is silently dropped).

    Other multi-frame formats (animated GIF/PNG/WebP, MPO) render only their first frame: extra frames there are
    previews, depth or gain maps rather than pages, and a warning is logged so the fact is not invisible."""
    rendered: List[Tuple[bytes, int, int, float]] = []
    budget = _PixelBudget(settings)
    try:
        with Image.open(path) as img:
            frames = getattr(img, "n_frames", 1)
            multipage = img.format == "TIFF" and frames > 1
            if frames > 1 and not multipage:
                logger.warning("%s: %s image has %d frames; only the first is rendered", doc_id, img.format, frames)
            if multipage and frames > settings.max_pages:
                raise UnsupportedDocumentError(f"TIFF has more than {settings.max_pages} pages")
            for index, frame in enumerate(ImageSequence.Iterator(img) if multipage else [img]):
                what = f"TIFF page {index + 1}" if multipage else "image"
                _check_pixels(frame.width, frame.height, settings, what)  # before the pixels are decoded
                budget.spend(frame.width * frame.height, what)
                frame.load()
                rendered.append(_frame_to_png(frame))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, SyntaxError) as exc:
        raise UnsupportedDocumentError("unsupported or corrupt image") from exc
    return [_make_page(doc_id, n, png, w, h, rot, settings) for n, (png, w, h, rot) in enumerate(rendered, 1)]


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
