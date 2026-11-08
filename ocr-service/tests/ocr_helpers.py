"""Helpers shared by the OCR tests (fictional documents only)."""
import io

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from app import ocr_core


def tesseract_available() -> bool:
    return ocr_core.resolve_engine().available


def text_png_bytes(lines, size=(1240, 700), font_size=44) -> bytes:
    """White page with black text lines, large enough for reliable OCR."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=font_size)
    for i, text in enumerate(lines):
        draw.text((80, 100 + i * 200), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def text_pdf_bytes(pages_text) -> bytes:
    """One A4 page per entry; each entry is a list of text lines."""
    doc = pymupdf.open()
    for lines in pages_text:
        page = doc.new_page(width=595, height=842)
        for i, text in enumerate(lines):
            page.insert_text((60, 120 + i * 90), text, fontsize=30)
    data = doc.tobytes()
    doc.close()
    return data


def fake_tsv(rows):
    """Build a pytesseract image_to_data dict (level-5 word rows plus a few structural rows).

    rows: (block, par, line, word, left, top, width, height, conf, text)
    """
    cols = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text"]
    data = {c: [] for c in cols}

    def add(*values):
        for c, v in zip(cols, values):
            data[c].append(v)

    add(1, 1, 0, 0, 0, 0, 0, 0, 1000, 500, -1, "")  # page row
    for b, p, ln, w, left, top, width, height, conf, text in rows:
        add(5, 1, b, p, ln, w, left, top, width, height, conf, text)
    return data
