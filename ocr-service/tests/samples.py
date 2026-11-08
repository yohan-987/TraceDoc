"""Generated, entirely fictional sample documents with known field strings (ID card, form, statement)."""
import io
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

Sample = Tuple[bytes, List[str]]


def _render(lines: List[str], size=(1240, 760), font_size=38) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=font_size)
    draw.rectangle([20, 20, size[0] - 20, size[1] - 20], outline="black", width=3)
    for i, text in enumerate(lines):
        draw.text((70, 60 + i * 85), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def id_card() -> Sample:
    lines = ["REPUBLIC OF EXAMPLELAND", "IDENTITY CARD", "Surname: EXAMPLE", "Given names: JANE ALICE",
             "Date of birth: 12/08/1990", "ID number: AB1234567", "Expires: 30/06/2030"]
    return _render(lines), ["IDENTITY CARD", "EXAMPLE", "JANE ALICE", "12/08/1990", "AB1234567", "30/06/2030"]


def form() -> Sample:
    lines = ["APPLICATION FORM", "Full name: John Q Sample", "Address: 12 Fictional Road", "Phone: 555-0142",
             "Employer: Sample Industries", "Signature date: 01/09/2026"]
    return _render(lines), ["APPLICATION FORM", "John Q Sample", "12 Fictional Road", "555-0142",
                            "Sample Industries", "01/09/2026"]


def statement() -> Sample:
    lines = ["ACCOUNT STATEMENT", "Account no: 0012345678", "Opening balance 1,250.00",
             "Salary credit 2,400.00", "Rent payment 800.00", "Closing balance 2,850.00"]
    return _render(lines), ["ACCOUNT STATEMENT", "0012345678", "1,250.00", "2,400.00", "800.00", "2,850.00"]


def text_lines(kind: str) -> List[str]:
    """Plain text lines of a sample (for PDF text-layer tests)."""
    return {"id": ["IDENTITY CARD", "Surname: EXAMPLE", "ID number: AB1234567"],
            "form": ["APPLICATION FORM", "Full name: John Q Sample", "Phone: 555-0142"],
            "statement": ["ACCOUNT STATEMENT", "Account no: 0012345678", "Closing balance 2,850.00"]}[kind]


def detection_rate(page_words: List[str], known: List[str]) -> float:
    """Share of known strings found in the OCR text (case- and whitespace-insensitive)."""
    haystack = "".join(page_words).lower().replace(" ", "")
    hits = [k for k in known if k.lower().replace(" ", "") in haystack]
    return len(hits) / len(known)
