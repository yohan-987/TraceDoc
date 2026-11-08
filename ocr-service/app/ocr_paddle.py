"""PaddleOCR adapter (primary OCR engine; Tesseract in app/ocr_core.py is the fallback).

Pydantic-free like ocr_core: it returns plain dicts in OcrWord / OcrLine shape.

* The PaddleOCR object is created once per process (model loading is slow) and reused.
* Supports the PaddleOCR 3.x API (``predict``) and the 2.x API (``ocr``); it could not be tested
  against either in the environment this was written in, so the parser is covered by tests that feed it
  both result shapes, and any real failure falls back to Tesseract.
* PaddleOCR returns text LINES, not words. Words are produced by splitting a line on whitespace and
  sharing the line box in proportion to character count, so word boxes are ESTIMATES. The line score is
  used as the confidence of each of its words.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, UnidentifiedImageError

from .ocr_core import OcrPageError, assemble_lines, engine_policy, normalize_box

logger = logging.getLogger(__name__)

ENGINE_NAME = "paddleocr"


class PaddleUnavailable(Exception):
    """PaddleOCR is not installed or could not be initialised."""


class PaddleFailure(Exception):
    """PaddleOCR failed while processing a page (caller should fall back)."""


@dataclass(frozen=True)
class PaddleStatus:
    available: bool
    version: Optional[str] = None
    reason: Optional[str] = None


_init_lock = threading.Lock()
_infer_lock = threading.Lock()  # PaddleOCR inference is not assumed thread-safe: one page at a time
_state: Optional[Tuple[Any, PaddleStatus]] = None


def paddle_lang() -> str:
    return os.environ.get("PADDLE_LANG", "en").strip() or "en"


def mkldnn_enabled() -> bool:
    """oneDNN (MKL-DNN) acceleration. OFF by default: PaddlePaddle 3.3.x has an oneDNN/PIR regression on
    x86-64 CPUs that makes inference raise "ConvertPirAttribute2RuntimeAttribute not support
    [pir::ArrayAttribute<pir::DoubleAttribute>]" (PaddlePaddle/Paddle#77340, PaddlePaddle/PaddleOCR#18162).
    ``enable_mkldnn=False`` avoids that code path. Set PADDLE_ENABLE_MKLDNN=1 to turn it back on."""
    return os.environ.get("PADDLE_ENABLE_MKLDNN", "0").strip().lower() in ("1", "true", "yes", "on")


def _create_engine() -> Tuple[Any, str]:
    try:
        import paddleocr  # heavy import, only when actually needed
    except ImportError as exc:
        raise PaddleUnavailable("PaddleOCR is not installed (see requirements-paddle.txt)") from exc
    version = str(getattr(paddleocr, "__version__", "unknown"))
    lang = paddle_lang()
    mkldnn = mkldnn_enabled()
    try:  # PaddleOCR 3.x arguments
        engine = paddleocr.PaddleOCR(lang=lang, use_doc_orientation_classify=False,
                                     use_doc_unwarping=False, use_textline_orientation=False,
                                     enable_mkldnn=mkldnn)
    except Exception:  # noqa: BLE001 - 2.x does not know those arguments
        try:
            engine = paddleocr.PaddleOCR(lang=lang, use_angle_cls=False, show_log=False, enable_mkldnn=mkldnn)
        except Exception as exc:  # noqa: BLE001
            raise PaddleUnavailable(f"PaddleOCR could not be initialised ({type(exc).__name__}: {exc})"[:300]) from exc
    return engine, version


def get_paddle() -> Tuple[Optional[Any], PaddleStatus]:
    """(engine, status). Initialised at most once per process; a failed init is remembered too."""
    global _state
    with _init_lock:
        if _state is None:
            try:
                engine, version = _create_engine()
                _state = (engine, PaddleStatus(True, version=version))
                logger.info("PaddleOCR %s initialised (lang=%s, oneDNN=%s)", version, paddle_lang(), mkldnn_enabled())
            except PaddleUnavailable as exc:
                _state = (None, PaddleStatus(False, reason=str(exc)))
                logger.warning("PaddleOCR unavailable: %s", exc)
            except Exception as exc:  # noqa: BLE001
                _state = (None, PaddleStatus(False, reason=f"PaddleOCR init failed ({type(exc).__name__})"))
                logger.exception("PaddleOCR init failed")
        return _state


def reset_paddle() -> None:
    """Forget the cached engine (tests only)."""
    global _state
    with _init_lock:
        _state = None


def start_warmup() -> None:
    """Initialise PaddleOCR in a background thread so the first request does not pay the model-load time."""
    if engine_policy() == "tesseract":
        return
    threading.Thread(target=get_paddle, name="paddle-warmup", daemon=True).start()


# ----------------------------------------------------------------------------- result parsing

def _tolist(value: Any) -> List[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):  # numpy
        value = value.tolist()
    return list(value)


def _bounds(shape: Any) -> Optional[Tuple[float, float, float, float]]:
    """Polygon [[x, y], ...] or flat [x0, y0, x1, y1] -> (x0, y0, x1, y1)."""
    pts = _tolist(shape)
    if not pts:
        return None
    try:
        if isinstance(pts[0], (int, float)):
            return (float(pts[0]), float(pts[1]), float(pts[2]), float(pts[3])) if len(pts) == 4 else None
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
    except (TypeError, ValueError, IndexError):
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _as_mapping(page_result: Any) -> Optional[Dict[str, Any]]:
    """PaddleOCR 3.x page results are dict-like (or expose .json); 2.x pages are lists."""
    if isinstance(page_result, dict):
        data = page_result
    else:
        data = getattr(page_result, "json", None)
        if not isinstance(data, dict):
            return None
    inner = data.get("res")
    return inner if isinstance(inner, dict) else data


def _detections(results: Any) -> List[Tuple[Optional[Tuple[float, float, float, float]], str, float]]:
    found = []
    for page_result in _tolist(results):
        if page_result is None:
            continue
        data = _as_mapping(page_result)
        if data is not None:  # 3.x
            texts = _tolist(data.get("rec_texts"))
            scores = _tolist(data.get("rec_scores"))
            shapes = _tolist(data.get("rec_polys")) or _tolist(data.get("dt_polys")) or _tolist(data.get("rec_boxes"))
            for i, text in enumerate(texts):
                shape = shapes[i] if i < len(shapes) else None
                score = scores[i] if i < len(scores) else 0.0
                found.append((_bounds(shape) if shape is not None else None, str(text), score))
        else:  # 2.x: [[poly, (text, score)], ...]
            for item in _tolist(page_result):
                try:
                    shape, (text, score) = item[0], item[1]
                except (TypeError, ValueError, IndexError):
                    continue
                found.append((_bounds(shape), str(text), score))
    return found


def _score(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return min(1.0, max(0.0, value))


def split_line_into_words(text: str, bounds: Tuple[float, float, float, float], score: float,
                          width_px: int, height_px: int) -> List[Dict[str, Any]]:
    """Estimate word boxes by sharing the line box in proportion to character count."""
    tokens = text.split()
    if not tokens:
        return []
    x0, y0, x1, y1 = bounds
    total = len(" ".join(tokens))
    offset = 0
    words = []
    for token in tokens:
        left = x0 + (x1 - x0) * offset / total
        right = x0 + (x1 - x0) * (offset + len(token)) / total
        offset += len(token) + 1
        box = normalize_box(left, y0, right - left, y1 - y0, width_px, height_px)
        if box is not None:
            words.append({"text": token, "bbox": box, "ocrConfidence": score})
    return words


def parse_paddle_result(results: Any, width_px: int, height_px: int) -> Dict[str, Any]:
    """PaddleOCR 3.x or 2.x output -> {widthPx, heightPx, words, lines}. Empty text and boxless rows are skipped."""
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for bounds, text, raw_score in _detections(results):
        if bounds is None or not text.strip():
            continue
        words = split_line_into_words(text, bounds, _score(raw_score), width_px, height_px)
        if words:
            groups[len(groups)] = words  # one PaddleOCR detection = one line, engine reading order
    words, lines = assemble_lines(groups)
    return {"widthPx": width_px, "heightPx": height_px, "words": words, "lines": lines}


# ----------------------------------------------------------------------------- running

def ocr_image_file(path: Path) -> Dict[str, Any]:
    """OCR one canonical page PNG. OcrPageError: the image itself is unreadable (do not fall back).
    PaddleFailure: the engine failed (caller falls back to Tesseract)."""
    engine, status = get_paddle()
    if engine is None:
        raise PaddleFailure(status.reason or "PaddleOCR unavailable")
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise OcrPageError("page image is unreadable or corrupted") from exc
    try:
        with _infer_lock:
            if hasattr(engine, "predict"):
                results = list(engine.predict(str(path)))
            else:
                results = list(engine.ocr(str(path), cls=False))
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "ConvertPirAttribute2RuntimeAttribute" in str(exc):
            hint = (" [oneDNN/PIR bug in PaddlePaddle 3.3.x; PADDLE_ENABLE_MKLDNN must be 0]" if mkldnn_enabled()
                    else " [oneDNN/PIR error although oneDNN is disabled here: see README Troubleshooting]")
        raise PaddleFailure(f"PaddleOCR failed ({type(exc).__name__}: {exc}){hint}"[:400]) from exc
    try:
        return parse_paddle_result(results, width, height)
    except Exception as exc:  # noqa: BLE001
        raise PaddleFailure(f"could not parse PaddleOCR output ({type(exc).__name__})") from exc
