"""O2 regression tests found by stress testing (see STRESS_TEST_REPORT.md)."""
import pytest

from app.ocr_core import normalize_box, parse_tsv


# ---------------------------------------------------------------- OCR parsing

def test_parse_tsv_survives_hostile_columns():
    # row 0 is fine, row 1 has a non-numeric level, row 2 an infinite level (int(inf) -> OverflowError), row 3 sits beyond the
    # end of the 'conf' column (IndexError): none of them may raise, and the good row must survive
    data = {"level": [5, "x", float("inf"), 5], "text": ["a", "b", "c", "d"], "conf": [90, 90, 90], "left": [1, 1, 1, 1],
            "top": [1, 1, 1, 1], "width": [5, 5, 5, 5], "height": [5, 5, 5, 5], "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 1, 1], "line_num": [1, 1, 1, 1]}
    out = parse_tsv(data, 100, 100)
    assert [w["text"] for w in out["words"]] == ["a"]


def test_normalize_box_rejects_non_finite_values():
    assert normalize_box(float("nan"), 0, 10, 10, 100, 100) is None  # NaN used to become a full-width box
    assert normalize_box(0, 0, float("inf"), 10, 100, 100) is None
    assert normalize_box(10, 10, 20, 20, 100, 100) == pytest.approx((0.1, 0.1, 0.2, 0.2))
