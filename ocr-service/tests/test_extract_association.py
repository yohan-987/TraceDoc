"""Label-to-value association (O4 fix): values below / beside labels, labels and format hints never returned as values.

Every page is synthetic (fictional names, numbers and amounts). The layouts reproduce what live OCR of the dev samples showed:
labels on one row ("DATE OF BIRTH  ISSUED"), their values on the row below, a printed format hint after a label
("Date of birth (YYYY-MM-DD)"), OCR noise from a photo to the left of the name, a heading on the same row as a label, and a
second column's label-plus-value pair beside an account number.
"""
import re

import pytest

from app.extract import extract_fields
from app.extract_normalize import normalize
from app.extract_schema import load_schema, load_settings
from tests.classifier_pages import ocr_page


@pytest.fixture(scope="module")
def settings():
    return load_settings()


def run(lines, doc_type, settings, **kw):
    out = extract_fields(ocr_page(lines, **kw), load_schema(doc_type), settings)
    return {r.name: r for r in out.results}, out.missing


# ----------------------------------------------------------------------------- id card: values BELOW their labels

ID_BELOW = ["REPUBLIC OF EXAMPLELAND SAMPLE",
            [("NAME", 400)],
            [("TOOLS ODS", 80), ("MARISOL TESTWELL", 400)],        # OCR noise from the photo, left of the name column
            [("DATE OF BIRTH", 400), ("ISSUED", 800)],
            [("07 MAR 1990", 400), ("01 JAN 2024", 800)],
            [("ID NUMBER", 400), ("EXPIRES", 800)],
            [("XMP-1234-5678-ZZ", 400), ("01 JAN 2034", 800)]]


def test_id_card_values_below_labels_not_the_neighbouring_label(settings):
    got, missing = run(ID_BELOW, "id_card", settings, width=1011, height=638)
    assert got["dob"].rawValue == "07 MAR 1990" and got["dob"].normalizedValue == "1990-03-07"      # was 'ISSUED'
    assert got["id_number"].rawValue == "XMP-1234-5678-ZZ"                                           # was 'EXPIRES'
    assert got["full_name"].rawValue == "MARISOL TESTWELL"                                           # was missing; no photo noise
    for name in ("dob", "id_number", "full_name"):
        assert got[name].validationStatus == "VALID"
        assert "placement=below" in got[name].notes


def test_id_card_bare_name_label_without_colon_is_an_anchor(settings):
    got, _ = run([[("NAME", 400)], [("ADA EXAMPLESON", 400)]], "id_card", settings, width=1011, height=638)
    assert got["full_name"].rawValue == "ADA EXAMPLESON"


# ----------------------------------------------------------------------------- application form

FORM_COLUMNS = ["Personal Loan Application",
                [("Application date", 80)],
                [("Full name", 80), ("ID number", 400), ("Date of birth (YYYY-MM-DD)", 800)],
                [("Test Applicant", 80), ("XMP-9876-5432-QQ", 400), ("1991-04-23", 800)],
                [("Permanent address", 80)],
                [("12 Fictional Road, Example Town 000000", 80)],
                [("Requested loan amount", 80), ("Term", 600)],
                [("USD 12,500.00", 80), ("24 months", 600)]]


def test_form_full_name_id_dob_and_amount_from_columns(settings):
    got, missing = run(FORM_COLUMNS, "application_form", settings)
    assert got["full_name"].rawValue == "Test Applicant"                      # was 'ID number'
    assert got["id_number"].rawValue == "XMP-9876-5432-QQ"                    # was not extracted
    assert got["dob"].rawValue == "1991-04-23" and got["dob"].normalizedValue == "1991-04-23"   # was '(YYYY-MM-DD)'
    assert got["requested_amount"].normalizedValue == "12500.00"              # was 'Term'
    assert got["address_permanent"].rawValue.startswith("12 Fictional Road")
    assert all(got[n].validationStatus == "VALID" for n in ("full_name", "id_number", "dob", "requested_amount"))


def test_format_hint_after_label_is_skipped_on_the_same_line(settings):
    got, _ = run(["Date of birth (YYYY-MM-DD): 1983-01-02"], "application_form", settings)
    assert got["dob"].rawValue == "1983-01-02"


def test_right_aligned_label_finds_its_value_under_it(settings):
    lines = [[("Date", 1000)], [("2026-05-02", 960)], [("Applicant signature", 80), ("Date", 700)], [("2026-05-03", 700)]]
    got, _ = run(lines, "application_form", settings)
    assert got["application_date"].rawValue == "2026-05-02"                   # the top-most label, by geometry


# ----------------------------------------------------------------------------- bank statement

STATEMENT = [[("Account holder", 80), ("Account summary", 700)],
             [("Test Holder", 80)],
             [("Account number", 80)],
             [("AC-0123-456789", 80), ("Total credits", 700), ("1,000.00", 900)]]


def test_statement_holder_is_not_the_heading_on_the_label_row(settings):
    got, _ = run(STATEMENT, "bank_statement", settings)
    assert got["account_holder"].rawValue == "Test Holder"                    # was 'Account summary'


def test_account_number_keeps_its_printed_prefix_and_ignores_the_next_column(settings):
    got, _ = run(STATEMENT, "bank_statement", settings)
    assert got["account_number"].rawValue == "AC-0123-456789"
    assert got["account_number"].normalizedValue == "AC-0123-456789"
    assert got["account_number"].validationStatus == "VALID"


def test_purely_numeric_account_number_is_still_digits_only(settings):
    got, _ = run(["Account number: 0000 1234 5678"], "bank_statement", settings)
    assert got["account_number"].normalizedValue == "000012345678"


def test_account_normalizer():
    assert normalize("account", "NSB-5404-470671", None) == ("NSB-5404-470671", False)
    assert normalize("account", "NSB - 5404 - 470671", None) == ("NSB-5404-470671", False)
    assert normalize("account", "5404 470671", None) == ("5404470671", False)


# ----------------------------------------------------------------------------- a label is never a value

_LABELS = {"issued", "expires", "id number", "term", "(yyyy-mm-dd)", "account summary", "date of birth", "full name",
           "date", "address", "name", "purpose", "account number", "signature"}


@pytest.mark.parametrize("doc_type,lines,kw", [
    ("id_card", ID_BELOW, dict(width=1011, height=638)),
    ("id_card", [[("DATE OF BIRTH", 400), ("ISSUED", 800)], [("ID NUMBER", 400), ("EXPIRES", 800)]], dict(width=1011, height=638)),
    ("application_form", FORM_COLUMNS, {}),
    ("application_form", [[("Full name", 80), ("ID number", 400), ("Date of birth (YYYY-MM-DD)", 800)],
                          [("Requested loan amount", 80), ("Term", 600)]], {}),
    ("bank_statement", STATEMENT, {}),
    ("bank_statement", [[("Account holder", 80), ("Account summary", 700)], [("Account number", 80)]], {}),
])
def test_a_label_or_format_hint_is_never_returned_as_a_value(settings, doc_type, lines, kw):
    got, missing = run(lines, doc_type, settings, **kw)
    for name, r in got.items():
        text = r.rawValue.strip().lower()
        assert text not in _LABELS, (name, r.rawValue)
        assert not re.fullmatch(r"\(.*[a-z].*\)", text), (name, r.rawValue)


def test_labels_only_page_reports_the_fields_missing_not_label_values(settings):
    got, missing = run([[("DATE OF BIRTH", 400), ("ISSUED", 800)], [("ID NUMBER", 400), ("EXPIRES", 800)]],
                       "id_card", settings, width=1011, height=638)
    assert "dob" in missing and "id_number" in missing
    assert "dob" not in got and "id_number" not in got


# ----------------------------------------------------------------------------- contract rules kept

def test_unvalidated_value_below_a_label_is_kept_as_invalid_format_not_dropped(settings):
    got, _ = run([[("DATE OF BIRTH", 400)], [("NOT A DATE", 400)]], "id_card", settings, width=1011, height=638)
    assert got["dob"].rawValue == "NOT A DATE"
    assert got["dob"].validationStatus == "INVALID_FORMAT" and got["dob"].normalizedValue is None
    assert 0.0 <= got["dob"].extractionConfidence <= 1.0 and 0.0 <= got["dob"].ocrConfidence <= 1.0


def test_a_valid_candidate_beats_a_nearer_invalid_one(settings):
    lines = [[("Date of birth", 80), ("see form", 500)], [("1980-02-03", 80)]]
    got, _ = run(lines, "application_form", settings)
    assert got["dob"].rawValue == "1980-02-03" and got["dob"].validationStatus == "VALID"


# ----------------------------------------------------------------------------- another person's label, OCR-garbled labels, skew

def test_a_guarantors_label_is_not_the_applicants_anchor(settings):
    """If the applicant's own 'Full name' label is unreadable, the guarantor's fields must not be returned as hers."""
    lines = [[("Fuil nane", 80), ("lD nunber", 400)],
             [("Test Applicant", 80), ("XMP-9876-5432-QQ", 400)],
             [("Guarantor full name", 80), ("Guarantor ID number", 400), ("Guarantor date of birth", 800)],
             [("Other Person", 80), ("XMP-1111-2222-AA", 400), ("1960-01-01", 800)]]
    got, missing = run(lines, "application_form", settings)
    for name in ("full_name", "id_number", "dob"):
        assert name in missing and name not in got, name


def test_ocr_garbled_labels_are_still_labels_not_values(settings):
    lines = [[("DATE OF BIRTH", 400), ("1SSUED", 800)], [("07 MAR 1990", 400), ("01 JAN 2024", 800)],
             [("ADDRESS", 400)], [("1 EXAMPLE STREET", 400)], [("TESTVILLE 00000", 400)], [("1D NUMBER", 400), ("EXPlRES", 800)],
             [("XMP-1234-5678-ZZ", 400), ("01 JAN 2034", 800)]]
    got, _ = run(lines, "id_card", settings, width=1011, height=638)
    assert got["dob"].rawValue == "07 MAR 1990" and got["dob"].validationStatus == "VALID"
    assert got["address_permanent"].rawValue == "1 EXAMPLE STREET TESTVILLE 00000"      # the '1D NUMBER' label row ends it
    # the garbled label is recognised as a label (it ends the address and is no value), but it is not used to FIND a field:
    # an id_number whose own label is unreadable is reported missing, never guessed
    assert "id_number" not in got


def test_value_row_of_a_slightly_rotated_scan_stays_one_row(settings):
    from app.models import OcrLine, OcrPage, OcrWord
    def w(t, x, y, line, ww=0.05, h=0.04):
        return OcrWord(text=t, bbox=(x, y, ww, h), ocrConfidence=0.9, lineIndex=line)
    words = [w("DATE", 0.32, 0.343, 0), w("OF", 0.37, 0.343, 0), w("BIRTH", 0.40, 0.343, 0),
             w("05", 0.324, 0.395, 1), w("SEP", 0.371, 0.389, 1), w("1984", 0.447, 0.381, 1, 0.075)]   # the baseline drifts upward
    lines = [OcrLine(text="DATE OF BIRTH", wordIndices=[0, 1, 2], bbox=(0.32, 0.343, 0.14, 0.04), ocrConfidence=0.9), OcrLine(text="05 SEP 1984", wordIndices=[3, 4, 5], bbox=(0.324, 0.381, 0.2, 0.06), ocrConfidence=0.9)]
    page = OcrPage(docId="FIX-1", page=1, widthPx=1011, heightPx=638, renderDpi=150.0, engine="fixture", engineVersion="t",
                   words=words, lines=lines)
    out = extract_fields(page, load_schema("id_card"), settings)
    got = {r.name: r for r in out.results}
    assert got["dob"].rawValue == "05 SEP 1984" and got["dob"].normalizedValue == "1984-09-05"


def test_heading_with_a_stray_mark_before_it_still_bounds_the_label_column(settings):
    """'. Account summary' (OCR noise before the heading) starts left of the label's right edge and sits on almost the same
    row as the label: it must still end the label's column, and 'summary' must not be read as the holder."""
    from app.models import OcrLine, OcrPage, OcrWord

    def w(t, x, y, line, ww, h):
        return OcrWord(text=t, bbox=(x, y, ww, h), ocrConfidence=0.9, lineIndex=line)
    words = [w("Account", 0.089, 0.132, 0, 0.04, 0.007), w("holder", 0.135, 0.132, 0, 0.035, 0.007),
             w(".", 0.121, 0.138, 1, 0.004, 0.004), w("Account", 0.589, 0.136, 1, 0.06, 0.011), w("summary", 0.655, 0.136, 1, 0.06, 0.012),
             w("Test", 0.089, 0.146, 2, 0.04, 0.010), w("Holder", 0.135, 0.146, 2, 0.05, 0.010)]
    lines = [OcrLine(text="Account holder", wordIndices=[0, 1], bbox=(0.089, 0.132, 0.081, 0.007), ocrConfidence=0.9),
             OcrLine(text=". Account summary", wordIndices=[2, 3, 4], bbox=(0.121, 0.136, 0.595, 0.012), ocrConfidence=0.9),
             OcrLine(text="Test Holder", wordIndices=[5, 6], bbox=(0.089, 0.146, 0.096, 0.010), ocrConfidence=0.9)]
    page = OcrPage(docId="FIX-1", page=1, widthPx=1240, heightPx=1754, renderDpi=150.0, engine="fixture", engineVersion="t",
                   words=words, lines=lines)
    got = {r.name: r for r in extract_fields(page, load_schema("bank_statement"), settings).results}
    assert got["account_holder"].rawValue == "Test Holder"
