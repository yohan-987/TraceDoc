"""Page classification from OCR text and layout (pure scoring; synthetic fictional pages)."""
import random
from dataclasses import replace

import pytest

from app.classifier_config import load_classifier_config
from app.classify import classify_page
from tests import classifier_pages as P

ALLOWED = {"id_card", "application_form", "bank_statement", "other", "UNKNOWN", "AMBIGUOUS"}


@pytest.fixture
def cfg():
    return load_classifier_config()


def kinds(result, doc_type):
    return {e.kind for e in result.evidence[doc_type]}


def test_id_card(cfg):
    r = classify_page(P.id_card(), cfg)
    assert r.docType == "id_card" and r.confidence > 0.6 and r.note is None


def test_application_form(cfg):
    r = classify_page(P.application_form(), cfg)
    assert r.docType == "application_form" and r.confidence > 0.6


def test_bank_statement(cfg):
    r = classify_page(P.bank_statement(), cfg)
    assert r.docType == "bank_statement" and r.confidence > 0.6
    assert {"table_rows", "table_header"} <= kinds(r, "bank_statement")  # transaction-table cues were used


@pytest.mark.parametrize("make", [P.other_invoice, P.other_letter])
def test_other_documents(cfg, make):
    r = classify_page(make(), cfg)
    assert r.docType == "other" and r.confidence > 0.5


def test_blank_page_is_unknown(cfg):
    r = classify_page(P.blank_page(), cfg)
    assert r.docType == "UNKNOWN" and r.confidence == 0.0 and "too little text" in r.note


@pytest.mark.parametrize("make", [P.random_text, P.gibberish])
def test_random_text_is_unknown(cfg, make):
    r = classify_page(make(), cfg)
    assert r.docType == "UNKNOWN" and r.confidence <= cfg.minScore


def test_stray_keywords_in_random_text_stay_unknown(cfg):
    r = classify_page(P.random_text(extra=["date", "name", "balance", "signature"]), cfg)
    assert r.docType == "UNKNOWN" and r.confidence <= cfg.minScore


def test_too_little_text_is_unknown_even_with_keywords(cfg):
    r = classify_page(P.ocr_page(["identity card"]), cfg)
    assert r.docType == "UNKNOWN"


def test_competing_scores_are_ambiguous(cfg):
    mixed = P.ocr_page([
        "Surname: SAMPLE", "Given names: JANE", "Date of birth: 12/08/1990", "Nationality: EXAMPLELANDER",
        "ID number: 000-FICTIONAL-01", "Applicant: Jane Sample", "Occupation: Clerk",
        "Employer: Sample Industries", "Marital status: Single", "Phone: 555-0142"])
    r = classify_page(mixed, cfg)
    assert r.docType == "AMBIGUOUS" and "within margin" in r.note
    top_two = sorted(r.scores.values(), reverse=True)[:2]
    assert top_two[0] - top_two[1] < cfg.ambiguityMargin
    assert r.confidence < top_two[0]  # a contested page is never reported as confidently as a clear one


def test_ambiguity_margin_is_configurable(cfg):
    page = P.application_form()
    assert classify_page(page, cfg).docType == "application_form"
    assert classify_page(page, replace(cfg, ambiguityMargin=0.9)).docType == "AMBIGUOUS"


def test_min_score_is_configurable(cfg):
    page = P.other_letter()
    assert classify_page(page, cfg).docType == "other"
    strict = classify_page(page, replace(cfg, minScore=0.99))
    assert strict.docType == "UNKNOWN" and strict.confidence <= 0.99


def test_layout_alone_never_creates_a_type(cfg):
    rows = [[("01/09/2026", 80), ("alpha", 250), ("100.00", 1000)] for _ in range(8)]
    page = P.ocr_page(rows + ["river stone window cloud engine garden pencil violet"])
    r = classify_page(page, cfg)
    assert r.docType == "UNKNOWN" and r.scores["bank_statement"] == 0.0


def test_card_aspect_ratio_adds_to_id_cards(cfg):
    card = classify_page(P.ocr_page(P.ID_CARD_LINES, width=1011, height=638), cfg)
    portrait = classify_page(P.ocr_page(P.ID_CARD_LINES, width=1240, height=1754), cfg)
    assert "aspect" in kinds(card, "id_card") and "aspect" not in kinds(portrait, "id_card")
    assert card.raw["id_card"] > portrait.raw["id_card"]


def test_transaction_rows_raise_the_statement_score(cfg):
    without = [ln for ln in P.STATEMENT_LINES if isinstance(ln, str)]
    r_with = classify_page(P.bank_statement(), cfg)
    r_without = classify_page(P.ocr_page(without), cfg)
    assert r_with.raw["bank_statement"] > r_without.raw["bank_statement"]
    assert "table_rows" not in kinds(r_without, "bank_statement")


def test_results_are_deterministic(cfg):
    page = P.bank_statement()
    assert classify_page(page, cfg) == classify_page(page, cfg)


def test_confidence_and_scores_always_within_zero_one(cfg):
    pages = [P.id_card(), P.application_form(), P.bank_statement(), P.other_invoice(), P.other_letter(),
             P.blank_page(), P.random_text(), P.gibberish()]
    rng = random.Random(11)
    phrases = ["date of birth", "account number", "balance", "signature", "invoice", "applicant", "debit", "credit",
               "surname", "declaration", "12/08/1990", "1,250.00", "identity card", "statement period", "lorem river",
               "requested amount", "opening balance", "nationality", "Dear Sir", "amount due", "branch", "qzx vlt"]
    for _ in range(300):
        lines = [" ".join(rng.choice(phrases) for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(0, 25))]
        pages.append(P.ocr_page(lines, width=rng.choice([1011, 1240]), height=rng.choice([638, 1754])))
    for page in pages:
        r = classify_page(page, cfg)
        assert r.docType in ALLOWED
        assert 0.0 <= r.confidence <= 1.0
        assert all(0.0 <= v <= 1.0 for v in r.scores.values())
        if r.docType == "UNKNOWN":
            assert r.confidence <= cfg.minScore  # UNKNOWN is never reported with real confidence


def test_explanations_never_use_accusatory_language(cfg):
    for page in (P.id_card(), P.bank_statement(), P.blank_page(), P.random_text()):
        r = classify_page(page, cfg)
        text = (r.note or "") + " ".join(e.short() for items in r.evidence.values() for e in items)
        assert "fraud" not in text.lower() and "forg" not in text.lower()
