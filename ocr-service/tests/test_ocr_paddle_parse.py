"""PaddleOCR result parsing (pure; PaddleOCR itself is not needed). Both 3.x and 2.x result shapes."""
import pytest

from app import ocr_paddle

V3 = [{
    "rec_texts": ["Hello World", "  ", "Second"],
    "rec_scores": [0.98, 0.9, 0.5],
    "rec_polys": [
        [[100, 50], [300, 50], [300, 90], [100, 90]],
        [[0, 0], [10, 0], [10, 10], [0, 10]],
        [[100, 200], [250, 200], [250, 240], [100, 240]],
    ],
}]
V2 = [[
    [[[100, 50], [300, 50], [300, 90], [100, 90]], ("Hello World", 0.98)],
    [[[100, 200], [250, 200], [250, 240], [100, 240]], ("Second", 0.5)],
]]


def _check(page):
    assert [w["text"] for w in page["words"]] == ["Hello", "World", "Second"]
    assert [w["lineIndex"] for w in page["words"]] == [0, 0, 1]
    assert [ln["text"] for ln in page["lines"]] == ["Hello World", "Second"]
    assert page["lines"][0]["wordIndices"] == [0, 1]
    # line box: x 100..300 of 1000, y 50..90 of 500
    assert page["lines"][0]["bbox"] == pytest.approx((0.1, 0.1, 0.2, 0.08))
    # word boxes share the line box by character count ("Hello" = 5 of 11 characters)
    assert page["words"][0]["bbox"] == pytest.approx((0.1, 0.1, 0.2 * 5 / 11, 0.08))
    assert page["words"][1]["bbox"][0] == pytest.approx(0.1 + 0.2 * 6 / 11)
    assert page["words"][0]["ocrConfidence"] == pytest.approx(0.98)


def test_parse_v3_dict_result():
    _check(ocr_paddle.parse_paddle_result(V3, 1000, 500))


def test_parse_v3_json_wrapper():
    class Result:  # object exposing .json == {"res": {...}}
        json = {"res": V3[0]}

    _check(ocr_paddle.parse_paddle_result([Result()], 1000, 500))


def test_parse_v2_list_result():
    _check(ocr_paddle.parse_paddle_result(V2, 1000, 500))


def test_blank_results():
    for blank in ([], [None], [[]], [{"rec_texts": [], "rec_scores": [], "rec_polys": []}]):
        page = ocr_paddle.parse_paddle_result(blank, 800, 600)
        assert page["words"] == [] and page["lines"] == []


def test_boxes_and_scores_stay_in_range():
    odd = [{"rec_texts": ["edge", "nan"], "rec_scores": [7.0, float("nan")],
            "rec_polys": [[[-50, 480], [120, 480], [120, 700], [-50, 700]], [[10, 10], [60, 10], [60, 40], [10, 40]]]}]
    page = ocr_paddle.parse_paddle_result(odd, 1000, 500)
    for w in page["words"]:
        x, y, bw, bh = w["bbox"]
        assert 0 <= x and 0 <= y and x + bw <= 1 + 1e-9 and y + bh <= 1 + 1e-9
        assert 0 <= w["ocrConfidence"] <= 1


def test_polygon_rotated_text_uses_bounding_box():
    page = ocr_paddle.parse_paddle_result(
        [{"rec_texts": ["tilt"], "rec_scores": [0.9], "rec_polys": [[[100, 60], [200, 50], [205, 90], [105, 100]]]}],
        1000, 500)
    x, y, w, h = page["words"][0]["bbox"]
    assert (x, y) == pytest.approx((0.1, 0.1)) and w == pytest.approx(0.105) and h == pytest.approx(0.1)


def test_rows_without_a_box_or_text_are_skipped():
    page = ocr_paddle.parse_paddle_result(
        [{"rec_texts": ["no box", "ok"], "rec_scores": [0.9, 0.9], "rec_polys": [None, [[0, 0], [50, 0], [50, 20], [0, 20]]]}],
        100, 100)
    assert [w["text"] for w in page["words"]] == ["ok"]
