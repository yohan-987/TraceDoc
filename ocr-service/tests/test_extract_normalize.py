"""Normalizers and format-only validators."""
import pytest

from app import extract_normalize as n


@pytest.mark.parametrize("raw,expected", [
    ("2004-08-12", "2004-08-12"), ("25/12/2004", "2004-12-25"), ("12/25/2004", "2004-12-25"),
    ("12 Aug 2004", "2004-08-12"), ("Aug 12, 2004", "2004-08-12"), ("5.5.2004", "2004-05-05"),
    ("31/02/2004", None), ("2004-13-01", None), ("12/08/04", None), ("12/O8/2004", None), ("not a date", None)])
def test_unambiguous_dates_and_invalid_dates(raw, expected):
    assert n.normalize_date(raw, "DMY")[0] == expected


def test_ambiguous_numeric_date_follows_the_configured_order():
    assert n.normalize_date("12/08/2004", "DMY") == ("2004-08-12", True)
    assert n.normalize_date("12/08/2004", "MDY") == ("2004-12-08", True)
    assert n.normalize_date("12/08/2004", None) == (None, False)        # never guessed without a configured order
    assert n.is_ambiguous_numeric_date("12/08/2004") and not n.is_ambiguous_numeric_date("25/12/2004")


@pytest.mark.parametrize("raw,expected", [
    ("1,250.00", "1250.00"), ("250,000", "250000.00"), ("2850.5", "2850.50"), ("Rs. 5,000", "5000.00"),
    ("$12.30", "12.30"), ("(2,850.50)", "-2850.50"), ("-1,000.00", "-1000.00"), ("INR 7", "7.00"),
    ("1.250,00", None), ("12,50", None), ("12.345", None), ("abc", None), ("", None)])
def test_amounts(raw, expected):
    assert n.normalize_amount(raw) == expected


def test_other_normalizers_never_change_characters_they_do_not_own():
    assert n.normalize_text("  JANE   ALICE \n SAMPLE ") == "JANE ALICE SAMPLE"
    assert n.normalize_digits("0000 1234-5678") == "000012345678"
    assert n.normalize_digits("00O0 1234") == "00O01234"       # no character is guessed
    assert n.normalize_id("AB 123 456") == "AB123456"
    assert n.normalize("none", "anything", None) == (None, False)


def test_validators_are_format_checks_only():
    V = n.validate
    assert V([("person_name", {})], "Jane Q. Sample", n.normalize_text("Jane Q. Sample")) == "VALID"
    assert V([("person_name", {})], "J4ne", "J4ne") == "INVALID_FORMAT"
    assert V([("date", {})], "12/08/2004", "2004-08-12") == "VALID"
    assert V([("date", {})], "31/02/2004", None) == "INVALID_FORMAT"
    assert V([("amount", {})], "1,250.00", "1250.00") == "VALID"
    assert V([("amount", {})], "1.250,00", None) == "INVALID_FORMAT"
    assert V([("address", {})], "12 Fictional Road", "12 Fictional Road") == "VALID"
    assert V([("address", {})], "12", "12") == "INVALID_FORMAT"
    assert V([("regex", {"pattern": "^[0-9]{6,20}$"})], "123", "123") == "INVALID_FORMAT"
    assert V([("regex", {"pattern": "^[0-9]{6,20}$"})], "0000123456", "0000123456") == "VALID"
    assert V([], "anything", "anything") == "NOT_CHECKED"
