"""Field extraction from OCR words/lines (pure engine, fictional pages)."""
from dataclasses import replace

import pytest

from app.extract import extract_fields
from app.extract_schema import load_schema, load_settings
from tests import extractor_pages as X


@pytest.fixture
def settings():
    return load_settings()


def run(page, doc_type, settings):
    out = extract_fields(page, load_schema(doc_type), settings)
    return {r.name: r for r in out.results}, out.missing


def words_of(page, line_text):
    """The OCR words of the line whose text starts with ``line_text``."""
    line = next(ln for ln in page.lines if ln.text.startswith(line_text))
    return [page.words[i] for i in line.wordIndices]


# ----- every supported document type and every requested field

@pytest.mark.parametrize("doc_type,make,expected", [
    ("id_card", X.id_card, X.ID_EXPECTED),
    ("application_form", X.application_form, X.FORM_EXPECTED),
    ("bank_statement", X.bank_statement, X.STATEMENT_EXPECTED)])
def test_each_document_type_extracts_every_field(settings, doc_type, make, expected):
    got, missing = run(make(), doc_type, settings)
    assert missing == []  # nothing the schema lists is missing on these pages
    for name, value in expected.items():
        assert got[name].normalizedValue == value, (name, got[name].rawValue)
        assert got[name].validationStatus == "VALID"


def test_raw_values_are_the_exact_ocr_text(settings):
    got, _ = run(X.id_card(), "id_card", settings)
    assert got["dob"].rawValue == "12/08/1990" and got["dob"].normalizedValue == "1990-08-12"
    assert got["address_permanent"].rawValue == "12 Fictional Road Example Town 000000"
    stmt, _ = run(X.bank_statement(), "bank_statement", settings)
    assert stmt["account_number"].rawValue == "0000 1234 5678" and stmt["account_number"].normalizedValue == "000012345678"
    assert stmt["opening_balance"].rawValue == "1,250.00" and stmt["opening_balance"].normalizedValue == "1250.00"


def test_id_card_name_can_be_assembled_from_given_names_and_surname(settings):
    lines = X.replace_line(X.ID_LINES, "Full name", "Surname: SAMPLE")
    lines.insert(3, "Given names: JANE ALICE")
    got, _ = run(X.id_card(lines), "id_card", settings)
    assert got["full_name"].rawValue == "JANE ALICE SAMPLE" and got["full_name"].strategy == "composite"
    assert got["full_name"].extractionConfidence < 0.9  # a composite is a weaker claim than a single labelled line


def test_application_form_signature_is_only_a_region_observation(settings):
    page = X.application_form()
    got, _ = run(page, "application_form", settings)
    sig = got["signature"]
    assert sig.rawValue == "" and sig.normalizedValue is None and sig.ocrConfidence == 0.0
    assert sig.validationStatus == "NOT_CHECKED" and sig.strategy == "anchor_region"
    assert any("no text was read" in n for n in sig.notes)
    label = words_of(page, "Signature")[0]
    assert sig.bbox[0] >= label.bbox[0] + label.bbox[2] - 1e-9          # starts right after the label
    assert 0 <= sig.bbox[0] and sig.bbox[0] + sig.bbox[2] <= 1 + 1e-9 and sig.bbox[1] + sig.bbox[3] <= 1 + 1e-9


# ----- missing fields are never fabricated

@pytest.mark.parametrize("remove,field", [("Date of birth", "dob"), ("ID number", "id_number"),
                                          ("Permanent address", "address_permanent")])
def test_missing_field_is_reported_not_invented(settings, remove, field):
    lines = X.without_line(X.ID_LINES, remove)
    if field == "address_permanent":
        lines = X.without_line(lines, "Example Town")
    got, missing = run(X.id_card(lines), "id_card", settings)
    assert field in missing and field not in got
    assert len(got) == 3


def test_label_without_a_value_is_missing_not_guessed(settings):
    lines = X.replace_line(X.ID_LINES, "ID number", "ID number: ________")
    got, missing = run(X.id_card(lines), "id_card", settings)
    assert "id_number" in missing


def test_page_with_no_labels_yields_no_fields(settings):
    page = X.id_card(["Some unrelated words here", "and nothing that looks like a form", "at all really no"])
    got, missing = run(page, "id_card", settings)
    assert got == {} and len(missing) == 4


# ----- confidences, flags and formats

def test_low_ocr_confidence_field_is_returned_and_flagged_by_its_values(settings):
    page = X.id_card()
    for w in words_of(page, "Date of birth"):
        w.ocrConfidence = 0.31
    got, _ = run(page, "id_card", settings)
    assert got["dob"].ocrConfidence == pytest.approx(0.31, abs=1e-3) and got["dob"].normalizedValue == "1990-08-12"
    assert got["dob"].ocrConfidence < settings.lowOcrConfidence       # visible, never dropped


def test_extraction_confidence_is_independent_of_ocr_confidence(settings):
    clean = X.id_card()
    noisy = X.id_card()
    for w in noisy.words:
        w.ocrConfidence = 0.12
    a, _ = run(clean, "id_card", settings)
    b, _ = run(noisy, "id_card", settings)
    for name in a:
        assert a[name].extractionConfidence == b[name].extractionConfidence
        assert b[name].ocrConfidence == pytest.approx(0.12)


def test_extraction_confidence_depends_on_how_the_value_was_found(settings):
    same_line, _ = run(X.id_card(), "id_card", settings)
    # label alone on its line, value on the next line: a weaker placement
    lines = X.replace_line(X.ID_LINES, "ID number", "ID number")
    lines.insert(lines.index("ID number") + 1, "000-FICTIONAL-01")
    below, _ = run(X.id_card(lines), "id_card", settings)
    assert below["id_number"].rawValue == "000-FICTIONAL-01"
    assert below["id_number"].extractionConfidence < same_line["id_number"].extractionConfidence


def test_invalid_date_is_returned_with_invalid_format(settings):
    for raw in ("31/02/1990", "12/O8/1990", "1/2/90"):
        lines = X.replace_line(X.ID_LINES, "Date of birth", f"Date of birth: {raw}")
        got, _ = run(X.id_card(lines), "id_card", settings)
        assert got["dob"].rawValue == raw and got["dob"].normalizedValue is None
        assert got["dob"].validationStatus == "INVALID_FORMAT", raw


def test_text_that_is_not_date_shaped_lowers_extraction_confidence(settings):
    good, _ = run(X.id_card(), "id_card", settings)
    lines = X.replace_line(X.ID_LINES, "Date of birth", "Date of birth: 12/O8/1990")
    bad, _ = run(X.id_card(lines), "id_card", settings)
    assert bad["dob"].extractionConfidence < good["dob"].extractionConfidence
    assert any("expected pattern" in n for n in bad["dob"].notes)


def test_invalid_amount_is_returned_with_invalid_format(settings):
    for raw in ("1.250,00", "twenty thousand"):
        lines = X.replace_line(X.FORM_LINES, "Requested amount", f"Requested amount: {raw}")
        got, _ = run(X.application_form(lines), "application_form", settings)
        assert got["requested_amount"].rawValue == raw and got["requested_amount"].normalizedValue is None
        assert got["requested_amount"].validationStatus == "INVALID_FORMAT"


def test_ambiguous_date_follows_the_configured_order_and_says_so(settings):
    got, _ = run(X.id_card(), "id_card", settings)           # 12/08/1990: day and month both <= 12
    assert got["dob"].normalizedValue == "1990-08-12" and got["dob"].dateOrderAssumed
    assert any("read as DMY" in n for n in got["dob"].notes)
    none_order = replace(settings, dateOrder=None)
    got, _ = run(X.id_card(), "id_card", none_order)
    assert got["dob"].normalizedValue is None and got["dob"].validationStatus == "NOT_CHECKED"
    assert got["dob"].rawValue == "12/08/1990"                  # raw is never altered
    lines = X.replace_line(X.ID_LINES, "Date of birth", "Date of birth: 25/12/1990")
    got, _ = run(X.id_card(lines), "id_card", none_order)
    assert got["dob"].normalizedValue == "1990-12-25" and not got["dob"].dateOrderAssumed


def test_several_amounts_on_a_totals_row_lower_confidence(settings):
    single, _ = run(X.bank_statement(), "bank_statement", settings)
    lines = X.replace_line(X.STATEMENT_LINES, "Total", "Total 920.50 2,404.20")
    multi, _ = run(X.bank_statement(lines), "bank_statement", settings)
    assert multi["total"].rawValue == "920.50"                   # schema says pick: first
    assert multi["total"].extractionConfidence < single["total"].extractionConfidence
    assert any("different candidate" in n for n in multi["total"].notes)


def test_balances_use_the_right_most_amount_on_a_table_row(settings):
    lines = X.replace_line(X.STATEMENT_LINES, "Opening balance", [("01/09/2026", 80), ("Opening balance", 250),
                                                                    ("1,250.00", 1000)])
    got, _ = run(X.bank_statement(lines), "bank_statement", settings)
    assert got["opening_balance"].normalizedValue == "1250.00"


# ----- bounding boxes

def test_field_box_is_the_union_of_the_value_words_only(settings):
    page = X.id_card()
    got, _ = run(page, "id_card", settings)
    line_words = words_of(page, "Date of birth")
    value_word = line_words[-1]
    assert got["dob"].bbox == pytest.approx(tuple(value_word.bbox))           # not the label words
    label_word = line_words[0]
    assert got["dob"].bbox[0] > label_word.bbox[0] + label_word.bbox[2] - 1e-9


def test_multi_line_address_box_spans_both_lines(settings):
    page = X.id_card()
    got, _ = run(page, "id_card", settings)
    first = words_of(page, "Permanent address")[-1]
    second = words_of(page, "Example Town")
    x, y, w, h = got["address_permanent"].bbox
    assert y == pytest.approx(first.bbox[1]) and y + h == pytest.approx(second[0].bbox[1] + second[0].bbox[3])
    assert x <= second[0].bbox[0] + 1e-9


@pytest.mark.parametrize("doc_type,make", [("id_card", X.id_card), ("application_form", X.application_form),
                                           ("bank_statement", X.bank_statement)])
def test_all_boxes_and_confidences_are_within_range(settings, doc_type, make):
    got, _ = run(make(), doc_type, settings)
    for r in got.values():
        x, y, w, h = r.bbox
        assert 0 <= x <= 1 and 0 <= y <= 1 and w > 0 and h > 0 and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9
        assert 0 <= r.ocrConfidence <= 1 and 0 <= r.extractionConfidence <= 1
        assert r.page == 1


def test_label_alone_with_value_to_its_right_on_the_same_row(settings):
    # two separate OCR lines on one visual row, as a detector that splits label and value would produce
    from app.models import OcrLine, OcrWord
    from tests.classifier_pages import ocr_page
    two = ocr_page(["Full name: JANE ALICE SAMPLE", [("ID number", 80)]], width=1011, height=638)
    # add the value as its own line on the same row as the label
    label_line = two.lines[1]
    label = two.words[label_line.wordIndices[-1]]
    value = OcrWord(text="000-FICTIONAL-01", bbox=(label.bbox[0] + label.bbox[2] + 0.1, label.bbox[1], 0.2, label.bbox[3]),
                    ocrConfidence=0.9, lineIndex=2)
    two.words.append(value)
    two.lines.append(OcrLine(text=value.text, bbox=value.bbox, ocrConfidence=0.9, wordIndices=[len(two.words) - 1]))
    got, _ = run(two, "id_card", settings)
    assert got["id_number"].rawValue == "000-FICTIONAL-01"
    assert any("placement=right" in n for n in got["id_number"].notes)


# ----- hygiene

def test_extraction_is_deterministic(settings):
    a, _ = run(X.bank_statement(), "bank_statement", settings)
    b, _ = run(X.bank_statement(), "bank_statement", settings)
    assert a == b


def test_explanations_never_use_accusatory_language(settings):
    for make, doc_type in ((X.id_card, "id_card"), (X.application_form, "application_form"),
                           (X.bank_statement, "bank_statement")):
        got, _ = run(make(), doc_type, settings)
        text = " ".join(" ".join(r.notes) + r.name + r.rawValue for r in got.values()).lower()
        assert "fraud" not in text and "forg" not in text
