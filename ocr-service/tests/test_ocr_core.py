"""Unit tests for OCR parsing and engine lookup. No Tesseract needed (pytesseract output is faked)."""
import pytest

from app import ocr_core
from tests.ocr_helpers import fake_tsv


@pytest.fixture(autouse=True)
def _restore_tesseract_cmd(monkeypatch):
    """ocr_image_file sets pytesseract's global command; put it back after each test."""
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd",
                        ocr_core.pytesseract.pytesseract.tesseract_cmd)


def test_words_lines_and_normalized_boxes():
    data = fake_tsv([
        (1, 1, 1, 1, 100, 50, 200, 40, 96.0, "Hello"),
        (1, 1, 1, 2, 320, 50, 180, 40, 90.0, "World"),
        (1, 1, 2, 1, 100, 200, 300, 40, 80.0, "Second"),
    ])
    page = ocr_core.parse_tsv(data, 1000, 500)
    assert [w["text"] for w in page["words"]] == ["Hello", "World", "Second"]
    assert page["words"][0]["bbox"] == pytest.approx((0.1, 0.1, 0.2, 0.08))
    assert page["words"][0]["ocrConfidence"] == pytest.approx(0.96)
    assert [w["lineIndex"] for w in page["words"]] == [0, 0, 1]
    assert len(page["lines"]) == 2
    first = page["lines"][0]
    assert first["text"] == "Hello World" and first["wordIndices"] == [0, 1]
    assert first["bbox"] == pytest.approx((0.1, 0.1, 0.4, 0.08))  # union of both words
    assert first["ocrConfidence"] == pytest.approx(0.93)  # mean of word confidences
    assert (page["widthPx"], page["heightPx"]) == (1000, 500)


def test_empty_and_invalid_tokens_are_skipped():
    data = fake_tsv([
        (1, 1, 1, 1, 10, 10, 50, 20, 95.0, "ok"),
        (1, 1, 1, 2, 70, 10, 50, 20, 95.0, "   "),   # whitespace only
        (1, 1, 1, 3, 130, 10, 50, 20, -1, "junk"),   # conf -1: not a recognised word
        (1, 1, 1, 4, 190, 10, 0, 20, 95.0, "flat"),  # zero width
    ])
    page = ocr_core.parse_tsv(data, 1000, 500)
    assert [w["text"] for w in page["words"]] == ["ok"]
    assert len(page["lines"]) == 1


def test_boxes_are_clipped_to_the_page_and_confidence_clamped():
    data = fake_tsv([
        (1, 1, 1, 1, -20, 480, 100, 100, 150.0, "edge"),   # sticks out left/bottom, conf > 100
        (1, 1, 1, 2, 2000, 10, 50, 20, 90.0, "outside"),   # fully outside: dropped
    ])
    page = ocr_core.parse_tsv(data, 1000, 500)
    assert [w["text"] for w in page["words"]] == ["edge"]
    x, y, w, h = page["words"][0]["bbox"]
    assert x == 0 and y + h <= 1 and x + w <= 1
    assert 0 <= page["words"][0]["ocrConfidence"] <= 1


def test_blank_page_gives_empty_result():
    page = ocr_core.parse_tsv(fake_tsv([]), 800, 600)
    assert page["words"] == [] and page["lines"] == []


def test_normalize_box_rejects_degenerate_input():
    assert ocr_core.normalize_box(0, 0, 10, 10, 0, 100) is None
    assert ocr_core.normalize_box(10, 10, 0, 5, 100, 100) is None


def test_missing_executable_is_unavailable_not_an_exception(monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\definitely\\not\\here\\tesseract.exe")
    status = ocr_core.resolve_engine()
    assert status.available is False and "TESSERACT_CMD" in status.reason


def test_not_found_anywhere_is_unavailable(monkeypatch):
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr_core.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr_core, "_WINDOWS_DEFAULT_PATHS", ())
    status = ocr_core.resolve_engine()
    assert status.available is False and "not found" in status.reason


def test_ocr_image_file_refuses_when_engine_unavailable(tmp_path):
    engine = ocr_core.EngineStatus(False, reason="nope")
    with pytest.raises(ocr_core.OcrUnavailableError):
        ocr_core.ocr_image_file(tmp_path / "x.png", engine, 150, 5)


def test_corrupted_image_is_a_page_error(tmp_path):
    bad = tmp_path / "page-1.png"
    bad.write_bytes(b"this is not a png")
    engine = ocr_core.EngineStatus(True, cmd="tesseract", version="x")
    with pytest.raises(ocr_core.OcrPageError):
        ocr_core.ocr_image_file(bad, engine, 150, 5)


def test_ragged_tesseract_columns_are_skipped_not_fatal():
    data = fake_tsv([(1, 1, 1, 1, 100, 50, 200, 40, 96.0, "Hello"), (1, 1, 1, 2, 320, 50, 180, 40, 90.0, "World")])
    data["conf"] = data["conf"][:2]          # shorter than the other columns
    data["text"] = data["text"][:2]
    page = ocr_core.parse_tsv(data, 1000, 500)
    assert [w["text"] for w in page["words"]] == ["Hello"]    # the row whose columns are missing is dropped


def test_overlong_or_odd_tesseract_cmd_is_unavailable_not_an_exception(monkeypatch):
    for value in ("a" * 5000, "C:\\" + "x\\" * 400 + "tesseract.exe"):  # environment values cannot contain NUL
        monkeypatch.setenv("TESSERACT_CMD", value)
        status = ocr_core.resolve_engine()
        assert status.available is False and status.reason


def test_engine_failure_is_not_reported_as_a_timeout(tmp_path, monkeypatch):
    import pytesseract
    from PIL import Image
    image = tmp_path / "page-1.png"
    Image.new("RGB", (40, 40), "white").save(image)
    engine = ocr_core.EngineStatus(True, cmd="tesseract", version="x")

    def failing(*a, **k):
        raise pytesseract.TesseractError(3, "boom")

    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", failing)
    with pytest.raises(ocr_core.OcrPageError) as failed:
        ocr_core.ocr_image_file(image, engine, 150, 5)
    assert "failed" in str(failed.value) and "timed out" not in str(failed.value)

    def timing_out(*a, **k):
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", timing_out)
    with pytest.raises(ocr_core.OcrPageError) as timed_out:
        ocr_core.ocr_image_file(image, engine, 150, 5)
    assert "timed out" in str(timed_out.value)
