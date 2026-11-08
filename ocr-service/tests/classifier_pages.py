"""Fictional OCR pages for the classifier tests, built through the same TSV parser as live OCR.

A line is either a string (words laid out left to right) or a list of (text, x_px) cells (a table row).
No real people, organisations or ID formats: every name and number below is invented.
"""
import io
import random
from typing import List, Sequence, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

from app import ocr_core
from app.models import OcrLine, OcrPage, OcrWord

Line = Union[str, Sequence[Tuple[str, int]]]
CHAR_W, GAP, LINE_H = 14, 14, 38


def ocr_page(lines: Sequence[Line], *, width: int = 1240, height: int = 1754, doc_id: str = "FIX-1",
             page: int = 1) -> OcrPage:
    groups = {}
    for i, line in enumerate(lines):
        cells = _flow(line) if isinstance(line, str) else list(line)
        words = []
        for text, x in cells:
            cursor = x
            for token in text.split():  # one OCR word per token, like Tesseract
                w = CHAR_W * len(token)
                box = ocr_core.normalize_box(cursor, 80 + i * LINE_H, w, 28, width, height)
                if box is not None:
                    words.append({"text": token, "bbox": box, "ocrConfidence": 0.95})
                cursor += w + GAP
        if words:
            groups[i] = words
    words, line_dicts = ocr_core.assemble_lines(groups)
    return OcrPage(docId=doc_id, page=page, widthPx=width, heightPx=height, renderDpi=150.0, engine="fixture",
                   engineVersion="test", words=[OcrWord(**w) for w in words],
                   lines=[OcrLine(**ln) for ln in line_dicts])


def _flow(text: str) -> List[Tuple[str, int]]:
    cells, x = [], 80
    for word in text.split():
        cells.append((word, x))
        x += CHAR_W * len(word) + GAP
    return cells


def blank_page(**kw) -> OcrPage:
    return ocr_page([], **kw)


ID_CARD_LINES = ["REPUBLIC OF EXAMPLELAND", "NATIONAL IDENTITY CARD", "Surname: SAMPLE", "Given names: JANE ALICE",
         "Date of birth: 12/08/1990", "Nationality: EXAMPLELANDER", "ID number: 000-FICTIONAL-01",
         "Date of expiry: 30/06/2030", "Sex: F"]


def id_card(**kw) -> OcrPage:
    return ocr_page(ID_CARD_LINES, width=1011, height=638, **kw)


FORM_LINES = ["LOAN APPLICATION FORM", "Application no: A-0001", "Full name of applicant: John Q Sample",
         "Date of birth: 01/02/1985", "Marital status: Single", "Permanent address: 12 Fictional Road",
         "Phone: 555-0142", "Employer: Sample Industries", "Occupation: Clerk", "Monthly income: 40,000.00",
         "Requested amount: 250,000.00", "Purpose of loan: Home repair",
         "Declaration: I hereby declare that the above is true and correct",
         "Signature: ________ Date of signing: 01/09/2026"]


def application_form(**kw) -> OcrPage:
    return ocr_page(FORM_LINES, **kw)


def _row(date, desc, debit, credit, balance):
    cells = [(date, 80), (desc, 250)]
    if debit:
        cells.append((debit, 700))
    if credit:
        cells.append((credit, 850))
    cells.append((balance, 1000))
    return cells

STATEMENT_LINES = ["SAMPLE BANK OF EXAMPLELAND", "ACCOUNT STATEMENT", "Account number: 000012345",
         "Statement period: 01/09/2026 to 30/09/2026", "Branch: Example Town",
         [("Date", 80), ("Description", 250), ("Debit", 700), ("Credit", 850), ("Balance", 1000)],
         _row("01/09/2026", "Opening balance", "", "", "1,250.00"),
         _row("02/09/2026", "Salary credit", "", "2,400.00", "3,650.00"),
         _row("05/09/2026", "Rent payment", "800.00", "", "2,850.00"),
         _row("12/09/2026", "Grocery store", "120.50", "", "2,729.50"),
         _row("20/09/2026", "Interest credited", "", "4.20", "2,733.70"),
         "Closing balance 2,733.70"]


def bank_statement(**kw) -> OcrPage:
    return ocr_page(STATEMENT_LINES, **kw)


INVOICE_LINES = ["TAX INVOICE", "Invoice no: INV-0042", "Bill to: Sample Traders", "Quantity Unit price Subtotal",
         "2 Widgets 10.00 20.00", "Amount due: 20.00", "Due date: 30/09/2026"]


def other_invoice(**kw) -> OcrPage:
    return ocr_page(INVOICE_LINES, **kw)


LETTER_LINES = ["To whom it may concern", "This is to certify that Jane Sample attended the workshop",
         "held at the Example Community Hall in the month of September", "Yours sincerely", "The Example Committee"]


def other_letter(**kw) -> OcrPage:
    return ocr_page(LETTER_LINES, **kw)


_VOCAB = ("lorem ipsum river stone window cloud engine garden pencil violet marble anchor canyon dolphin ember "
          "falcon glacier harbor island jungle kettle lantern meadow nectar orchid pebble quartz ripple saddle "
          "timber umbrella valley whisper yonder zephyr").split()


def random_text(seed: int = 7, n_words: int = 80, extra: Sequence[str] = (), **kw) -> OcrPage:
    """Seeded random vocabulary with no document keywords (optionally a few stray words in ``extra``)."""
    rng = random.Random(seed)
    words = [rng.choice(_VOCAB) for _ in range(n_words)] + list(extra)
    rng.shuffle(words)
    per_line = 8
    return ocr_page([" ".join(words[i:i + per_line]) for i in range(0, len(words), per_line)], **kw)


def gibberish(seed: int = 3, n_words: int = 70, **kw) -> OcrPage:
    rng = random.Random(seed)
    letters = "bcdfghjklmnpqrstvwxyz"
    words = ["".join(rng.choice(letters) for _ in range(rng.randint(3, 8))) for _ in range(n_words)]
    return ocr_page([" ".join(words[i:i + 8]) for i in range(0, len(words), 8)], **kw)


def render_png(lines: Sequence[Line], width: int, height: int, font_size: int = 30, scale: float = 1.0) -> bytes:
    """Draw the same fictional text into a white page image (for real-OCR tests); cells keep their x positions."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=font_size)
    for i, line in enumerate(lines):
        y = 60 + int(i * LINE_H * scale)
        if isinstance(line, str):
            draw.text((60, y), line, fill="black", font=font)
        else:
            for text, x in line:
                draw.text((int(x * scale) if scale != 1 else x, y), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
