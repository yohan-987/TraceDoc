"""Draw OCR / text-layer boxes over a canonical page image (pure Pillow; used by the debug endpoint)."""
from __future__ import annotations

import io
from typing import Any, Sequence, Tuple

from PIL import Image, ImageDraw

Color = Tuple[int, int, int]

WORD_HIGH: Color = (0, 170, 0)      # word, confidence >= 0.8: green
WORD_MID: Color = (255, 140, 0)     # word, 0.5 <= confidence < 0.8: orange
WORD_LOW: Color = (220, 0, 0)       # word, confidence < 0.5: red
LINE: Color = (0, 90, 255)          # line boxes: blue
TEXT_LAYER: Color = (200, 0, 200)   # PDF text-layer words: magenta (confidence is not meaningful)

LEGEND = ("blue=line; words: green conf>=0.8, orange 0.5-0.8, red <0.5; "
          "magenta=pdf_text_layer words (no confidence)")


def _pixel_box(bbox: Sequence[float], width: int, height: int) -> Tuple[int, int, int, int]:
    x, y, w, h = bbox
    return (round(x * width), round(y * height), round((x + w) * width), round((y + h) * height))


def word_color(confidence: float) -> Color:
    if confidence >= 0.8:
        return WORD_HIGH
    return WORD_MID if confidence >= 0.5 else WORD_LOW


def draw_boxes(image: Image.Image, page: Any, text_layer: bool = False) -> bytes:
    """PNG bytes of ``image`` (same size) with line boxes, then word boxes, drawn from the OcrPage ``page``."""
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    if not text_layer:
        for line in page.lines:
            draw.rectangle(_pixel_box(line.bbox, width, height), outline=LINE, width=2)
    for word in page.words:
        color = TEXT_LAYER if text_layer else word_color(word.ocrConfidence)
        draw.rectangle(_pixel_box(word.bbox, width, height), outline=color, width=2)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
