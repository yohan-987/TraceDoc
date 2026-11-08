"""O4 regression tests found by stress testing (see STRESS_TEST_REPORT.md)."""
import time

from app.extract import extract_fields
from app.extract_normalize import normalize_amount
from app.extract_schema import load_schema, load_settings
from app.models import OcrLine, OcrPage, OcrWord


def _page(lines):
    words, lns = [], []
    for li, text in enumerate(lines):
        y, idx = (0.02 + 0.012 * li) % 0.95, []
        for j, tok in enumerate(text.split()):
            words.append(OcrWord(text=tok, bbox=(min(0.05 + 0.06 * j, 0.95), y, 0.05, 0.01), ocrConfidence=0.9, lineIndex=li))
            idx.append(len(words) - 1)
        if idx:
            lns.append(OcrLine(text=text, bbox=(0.05, y, 0.9, 0.01), ocrConfidence=0.9, wordIndices=idx))
    return OcrPage(docId="d", page=1, widthPx=1000, heightPx=1400, renderDpi=150, engine="t", engineVersion="1", words=words, lines=lns)


def test_value_before_a_second_label_on_the_same_line_is_not_truncated():
    out = extract_fields(_page(["Full Name: John Smith Date of Birth: 12/08/2004"]), load_schema("id_card"), load_settings())
    got = {r.name: r.rawValue for r in out.results}
    assert got["full_name"] == "John Smith"  # used to be "John": the label regex swallowed the last word of the value
    assert got["dob"] == "12/08/2004"


def test_three_word_name_before_next_label():
    out = extract_fields(_page(["Full Name: Mary Jane Watson ID Number: AB12345"]), load_schema("id_card"), load_settings())
    got = {r.name: r.rawValue for r in out.results}
    assert got["full_name"] == "Mary Jane Watson" and got["id_number"] == "AB12345"


def test_thousands_of_label_lines_stay_fast():
    for doc_type, label in (("id_card", "Date of Birth"), ("application_form", "Permanent Address"), ("bank_statement", "Opening Balance")):
        start = time.monotonic()
        extract_fields(_page([label] * 8000), load_schema(doc_type), load_settings())
        assert time.monotonic() - start < 5, doc_type  # was ~48 s for 10,000 lines


def test_unicode_minus_in_amounts():
    assert normalize_amount("\u2212500.00") == "-500.00"
    assert normalize_amount("\u20131,234.50") == "-1234.50"
