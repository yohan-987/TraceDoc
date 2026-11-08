"""Tesseract access and output parsing for the OCR analyzer (O2).

Deliberately free of the pydantic models and of the analysis context: it works on plain dicts so the
parsing is easy to unit test. app/ocr.py turns these dicts into the shared OcrPage contract.
"""
from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytesseract
from PIL import Image, UnidentifiedImageError

ENGINE_NAME = "tesseract"
DEFAULT_LANG = "eng"
DEFAULT_PAGE_TIMEOUT_S = 25.0
# Only consulted when TESSERACT_CMD is unset and tesseract is not on PATH.
_WINDOWS_DEFAULT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

BBox = Tuple[float, float, float, float]


class OcrUnavailableError(Exception):
    """Tesseract (or its language data) cannot be used at all."""


class OcrPageError(Exception):
    """OCR failed for one page (unreadable image, timeout, engine error)."""


@dataclass(frozen=True)
class EngineStatus:
    available: bool
    cmd: Optional[str] = None
    version: Optional[str] = None
    lang: str = DEFAULT_LANG
    reason: Optional[str] = None


def page_timeout_s() -> float:
    try:
        value = float(os.environ.get("OCR_PAGE_TIMEOUT_S", DEFAULT_PAGE_TIMEOUT_S))
    except ValueError:
        return DEFAULT_PAGE_TIMEOUT_S
    return value if 0 < value < 3600 else DEFAULT_PAGE_TIMEOUT_S  # also rejects NaN and inf


def _is_file(path: str) -> bool:
    """Path.is_file(), but an over-long or malformed path (OSError / ValueError) is just 'not a file'."""
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _locate() -> Tuple[Optional[str], Optional[str]]:
    configured = os.environ.get("TESSERACT_CMD", "").strip().strip('"')
    if configured:
        if _is_file(configured):
            return configured, None
        return None, "TESSERACT_CMD does not point to an existing file"
    found = shutil.which("tesseract")
    if found:
        return found, None
    for candidate in _WINDOWS_DEFAULT_PATHS:
        if os.path.isfile(candidate):
            return candidate, None
    return None, ("Tesseract executable not found: install it and set TESSERACT_CMD "
                  "or add it to PATH")


def resolve_engine() -> EngineStatus:
    """Locate Tesseract and verify it runs and has the requested language data. Never raises."""
    lang = os.environ.get("OCR_LANG", DEFAULT_LANG).strip() or DEFAULT_LANG
    cmd, problem = _locate()
    if cmd is None:
        return EngineStatus(False, lang=lang, reason=problem)
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        version = str(pytesseract.get_tesseract_version())
        installed = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError:
        return EngineStatus(False, cmd=cmd, lang=lang, reason="Tesseract executable could not be started")
    except Exception as exc:  # noqa: BLE001 - any failure here means "cannot use Tesseract"
        return EngineStatus(False, cmd=cmd, lang=lang,
                            reason=f"could not query Tesseract ({type(exc).__name__})")
    missing = [code for code in lang.split("+") if code not in installed]
    if missing:
        return EngineStatus(False, cmd=cmd, version=version, lang=lang,
                            reason=f"Tesseract language data missing: {', '.join(missing)}")
    return EngineStatus(True, cmd=cmd, version=version, lang=lang)


def normalize_box(left: float, top: float, w: float, h: float,
                  width_px: int, height_px: int) -> Optional[BBox]:
    """Pixel box -> [x, y, w, h] in 0..1, clipped to the page. None if nothing is left."""
    if width_px <= 0 or height_px <= 0:
        return None
    if not all(math.isfinite(v) for v in (left, top, w, h)):
        return None  # NaN would otherwise slip through min/max and produce a full-width box
    x0 = max(0.0, left / width_px)
    y0 = max(0.0, top / height_px)
    x1 = min(1.0, (left + w) / width_px)
    y1 = min(1.0, (top + h) / height_px)
    if not (x1 > x0 and y1 > y0):
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _confidence(raw: Any) -> Optional[float]:
    """Tesseract reports 0..100, or -1 for rows that are not recognised words."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value < 0:
        return None
    return min(1.0, value / 100.0)


def _union(boxes: List[BBox]) -> BBox:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, min(1.0, x1) - x0, min(1.0, y1) - y0)


def parse_tsv(data: Dict[str, List[Any]], width_px: int, height_px: int) -> Dict[str, Any]:
    """Turn pytesseract's image_to_data dict into words and lines (OcrWord / OcrLine shapes).

    Words are Tesseract level-5 rows with non-empty text and a valid confidence. Lines are Tesseract's
    (block, paragraph, line) groups, in reading order; a line with no surviving words is dropped.
    """
    groups: Dict[Tuple[int, int, int], List[Dict[str, Any]]] = {}
    for i in range(len(data.get("text", []))):
        try:
            if int(data["level"][i]) != 5:
                continue
            text = str(data["text"][i]).strip()
            conf = _confidence(data["conf"][i])
            box = normalize_box(float(data["left"][i]), float(data["top"][i]),
                                float(data["width"][i]), float(data["height"][i]),
                                width_px, height_px)
            key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):  # OverflowError: int(inf)
            continue
        if not text or conf is None or box is None:
            continue
        groups.setdefault(key, []).append({"text": text, "bbox": box, "ocrConfidence": conf})

    words: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []
    for line_index, line_words in enumerate(groups.values()):
        indices = []
        for word in line_words:
            word["lineIndex"] = line_index
            indices.append(len(words))
            words.append(word)
        lines.append({
            "text": " ".join(w["text"] for w in line_words),
            "bbox": _union([w["bbox"] for w in line_words]),
            "ocrConfidence": sum(w["ocrConfidence"] for w in line_words) / len(line_words),
            "wordIndices": indices,
        })
    return {"widthPx": width_px, "heightPx": height_px, "words": words, "lines": lines}


def ocr_image_file(path: Path, engine: EngineStatus, dpi: float, timeout_s: float) -> Dict[str, Any]:
    """Run Tesseract on one canonical page PNG. Raises OcrPageError / OcrUnavailableError."""
    if not engine.available or not engine.cmd:
        raise OcrUnavailableError(engine.reason or "Tesseract unavailable")
    if timeout_s <= 0:
        raise OcrPageError("time budget exhausted")
    try:
        with Image.open(path) as img:
            img.load()
            image = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OcrPageError("page image is unreadable or corrupted") from exc
    pytesseract.pytesseract.tesseract_cmd = engine.cmd
    try:
        data = pytesseract.image_to_data(
            image, lang=engine.lang, config=f"--dpi {max(1, int(round(dpi)))}",
            output_type=pytesseract.Output.DICT, timeout=timeout_s)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailableError("Tesseract executable could not be started") from exc
    except pytesseract.TesseractError as exc:  # a RuntimeError subclass: must be tested before the timeout case
        raise OcrPageError(f"Tesseract failed (exit status {getattr(exc, 'status', '?')})") from exc
    except RuntimeError as exc:  # pytesseract raises RuntimeError("Tesseract process timeout")
        if "timeout" in str(exc).lower():
            raise OcrPageError(f"Tesseract timed out after {timeout_s:g}s") from exc
        raise OcrPageError("Tesseract failed (RuntimeError)") from exc
    except Exception as exc:  # noqa: BLE001 - anything else from the engine
        raise OcrPageError(f"Tesseract failed ({type(exc).__name__})") from exc
    return parse_tsv(data, image.width, image.height)
