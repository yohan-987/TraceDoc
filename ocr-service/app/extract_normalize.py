"""Value normalizers and FORMAT-ONLY validators for the field extractor.

Nothing here looks anything up: no identity, bank or government checks, no clock, no network.
rawValue is never changed; these functions only derive normalizedValue and a validation verdict.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Tuple

# Built-in pattern aliases usable as `pattern: "@date"` in schemas.
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
PATTERN_ALIASES = {
    "@date": (r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s?[/.\-]\s?\d{1,2}\s?[/.\-]\s?\d{2,4}"
              rf"|\d{{1,2}}\s?(?:{_MONTHS})[a-z]*\.?,?\s?\d{{2,4}}|(?:{_MONTHS})[a-z]*\.?\s\d{{1,2}},?\s\d{{2,4}}"),
    "@amount": r"(?<![\d.,/])[-(]?\s?(?:[$€£₹]|rs\.?|inr|usd|eur|gbp)?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\)?"
               r"(?![\d/]|\.\d|,\d)",
    "@digits": r"\d(?:[\d \-]*\d)?",
}

NORMALIZERS = ("text", "date", "amount", "digits", "id", "none")
VALIDATORS = ("person_name", "date", "amount", "address", "regex")


def resolve_pattern(pattern: str) -> str:
    return PATTERN_ALIASES.get(pattern, pattern)


def normalize_text(raw: str) -> Optional[str]:
    value = re.sub(r"\s+", " ", raw).strip()
    return value or None


def normalize_id(raw: str) -> Optional[str]:
    value = re.sub(r"\s+", "", raw)
    return value or None


def normalize_digits(raw: str) -> Optional[str]:
    """Account-number style: drop spaces and hyphens only (no characters are guessed or replaced)."""
    value = re.sub(r"[\s\-]", "", raw)
    return value or None


_MONTH_NUM = {m: i for i, m in enumerate(_MONTHS.split("|"), 1)}


def _make_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_date(raw: str, order: Optional[str]) -> Tuple[Optional[str], bool]:
    """(ISO date or None, order_was_assumed).

    Unambiguous inputs are normalized: ISO, month names, and numeric dates where one number exceeds 12.
    A numeric date with day and month both <= 12 and different is ambiguous: it is normalized only if ``order``
    ("DMY" or "MDY") is configured, and the second element reports that the order was assumed.
    Two-digit years are never guessed.
    """
    text = raw.strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return _make_date(int(m[1]), int(m[2]), int(m[3])), False
    m = re.fullmatch(rf"(\d{{1,2}})\s?({_MONTHS})[a-z]*\.?,?\s?(\d{{4}})", text)
    if m:
        return _make_date(int(m[3]), _MONTH_NUM[m[2]], int(m[1])), False
    m = re.fullmatch(rf"({_MONTHS})[a-z]*\.?\s(\d{{1,2}}),?\s(\d{{4}})", text)
    if m:
        return _make_date(int(m[3]), _MONTH_NUM[m[1]], int(m[2])), False
    m = re.fullmatch(r"(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{4})", text)
    if not m:
        return None, False
    a, b, year = int(m[1]), int(m[2]), int(m[3])
    if a > 12 and b <= 12:
        return _make_date(year, b, a), False          # day first, unambiguous
    if b > 12 and a <= 12:
        return _make_date(year, a, b), False          # month first, unambiguous
    if a == b:
        return _make_date(year, a, b), False          # same either way
    if order == "DMY":
        return _make_date(year, b, a), True
    if order == "MDY":
        return _make_date(year, a, b), True
    return None, False


def is_ambiguous_numeric_date(raw: str) -> bool:
    """A numeric date whose day and month are both <= 12 and different (valid under either reading)."""
    m = re.fullmatch(r"(\d{1,2})\s?[/.\-]\s?(\d{1,2})\s?[/.\-]\s?(\d{4})", raw.strip())
    if not m:
        return False
    a, b = int(m[1]), int(m[2])
    return 1 <= a <= 12 and 1 <= b <= 12 and a != b


_MINUS_SIGNS = {0x2212: "-", 0x2013: "-", 0x2014: "-"}
_CURRENCY = re.compile(r"^(?:[$€£₹]|rs\.?|inr|usd|eur|gbp)\s?|\s?(?:[$€£₹]|rs\.?|inr|usd|eur|gbp)$", re.I)


def normalize_amount(raw: str) -> Optional[str]:
    """Plain decimal string with two decimals, e.g. "2,850.5" -> "2850.50". No currency conversion.

    Accepted: optional currency symbol/code, optional minus or enclosing parentheses (negative), digits with optional
    comma thousands groups and an optional 1-2 digit decimal part. Anything else (for example "1.250,00") is not
    guessed and returns None.
    """
    text = raw.strip().translate(_MINUS_SIGNS)  # OCR often reads a hyphen-minus as U+2212 / an en or em dash
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    text = _CURRENCY.sub("", text).strip()
    if text.startswith("-"):
        negative, text = True, text[1:].strip()
    text = _CURRENCY.sub("", text).strip()
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?", text):
        return None
    try:
        value = Decimal(text.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if negative and value != 0:
        value = -value
    return format(value, "f")


def normalize(kind: str, raw: str, date_order: Optional[str]) -> Tuple[Optional[str], bool]:
    """(normalizedValue, date_order_assumed)."""
    if kind == "date":
        return normalize_date(raw, date_order)
    if kind == "amount":
        return normalize_amount(raw), False
    if kind == "digits":
        return normalize_digits(raw), False
    if kind == "id":
        return normalize_id(raw), False
    if kind == "text":
        return normalize_text(raw), False
    return None, False  # "none"


# ----------------------------------------------------------------------------- validators (format only)

_NAME = re.compile(r"[^\W\d_]+\.?(?:[ \-'’][^\W\d_]+\.?)*")


def _check(name: str, params: Mapping[str, Any], raw: str, normalized: Optional[str]) -> bool:
    value = normalized if normalized is not None else raw.strip()
    if name == "person_name":
        return bool(normalized) and len(normalized) >= 2 and _NAME.fullmatch(normalized) is not None
    if name in ("date", "amount"):
        return normalized is not None
    if name == "address":
        text = normalized or ""
        return len(text) >= 5 and bool(re.search(r"[^\W\d_]", text)) and (bool(re.search(r"\d", text)) or len(text.split()) >= 2)
    if name == "regex":
        return re.fullmatch(str(params["pattern"]), value) is not None
    raise ValueError(f"unknown validator {name!r}")


def validate(validators: Sequence[Tuple[str, Mapping[str, Any]]], raw: str, normalized: Optional[str]) -> str:
    """VALID if every validator passes, INVALID_FORMAT if any fails, NOT_CHECKED if there are none."""
    if not validators:
        return "NOT_CHECKED"
    return "VALID" if all(_check(n, p, raw, normalized) for n, p in validators) else "INVALID_FORMAT"
