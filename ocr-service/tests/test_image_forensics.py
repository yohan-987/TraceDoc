"""O8 image forensics: config, algorithms on synthetic pages, the three analyzers and /analyze.

Every page is generated here and fictional. Real-sample accuracy lives in test_image_forensics_samples.py.
"""
import io
import random
import string

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from app import image_forensics as algo
from app.analyzers.image_forensics import ImageCopyMoveAnalyzer, ImageElaAnalyzer, ImageNoiseAnalyzer
from app.context import AnalysisContext
from app.image_forensics_config import (DEFAULT_PATH, ImageForensicsConfigError, load_image_forensics_config)
from app.models import AnalyzeResponse
from app.registry import AnalyzerRegistry, run_analyzers
from app.render import render_document

W, H, PITCH = 1100, 1500, 60
FONT = ImageFont.load_default(size=30)


def _word(rng):
    return "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(rng.randint(4, 8)))


def make_page(seed=1, noise=3.0, rows=20, cols=4):
    """Fictional text page: ``rows`` lines of random (so all different) words on noisy paper."""
    rng = random.Random(seed)
    img = Image.new("L", (W, H), 238)
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            d.text((60 + c * 260, 80 + r * PITCH), _word(rng), fill=30, font=FONT)
    arr = np.asarray(img, dtype=np.float32)
    if noise:
        arr = arr + np.random.default_rng(seed).normal(0, noise, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")


def to_jpeg(img, q=75):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def iou(a, b):
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


@pytest.fixture
def cfg():
    return load_image_forensics_config()


@pytest.fixture
def analyze(settings, put_original):
    def _run(doc_id, data, ext, *analyzers):
        put_original(doc_id, f"original.{ext}", data)
        ctx = AnalysisContext(render_document(doc_id, settings), settings)
        reg = AnalyzerRegistry()
        for a in analyzers or (ImageElaAnalyzer, ImageNoiseAnalyzer, ImageCopyMoveAnalyzer):
            reg.register(a)
        return run_analyzers(ctx, reg, timeout_s=60)
    return _run


# ------------------------------------------------------------------------------------------ config
def test_default_config_loads_and_echoes_every_threshold(cfg):
    prov = cfg.as_provenance()
    assert prov["cfg.cmMinCoverage"] == cfg.cmMinCoverage and len(prov) == 30
    assert all(isinstance(v, (int, float, str)) for v in prov.values())


@pytest.mark.parametrize("edit,message", [
    (lambda d: d.pop("cmRatio"), "missing"),
    (lambda d: d.update(extra=1), "unknown"),
    (lambda d: d.update(cmRatio="0.8"), "must be a float"),
    (lambda d: d.update(cmRatio=0), "cmRatio"),
    (lambda d: d.update(elaQuality=0), "elaQuality"),
    (lambda d: d.update(cmMinCoverage=1.5), "cmMinCoverage"),
    (lambda d: d.update(maxWorkingSidePx=10), "maxWorkingSidePx"),
])
def test_invalid_config_is_rejected(tmp_path, edit, message):
    import json
    raw = json.loads(DEFAULT_PATH.read_text())
    edit(raw)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ImageForensicsConfigError, match=message):
        load_image_forensics_config(path)


def test_nan_and_missing_file_are_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(DEFAULT_PATH.read_text().replace('"cmRatio": 0.8', '"cmRatio": NaN'))
    with pytest.raises(ImageForensicsConfigError):
        load_image_forensics_config(path)
    with pytest.raises(ImageForensicsConfigError, match="not found"):
        load_image_forensics_config(tmp_path / "nope.json")


# ------------------------------------------------------------------------------------------ copy-move
def paste_row(img, src_y, dst_y, x0=40, x1=1060):
    out = img.copy()
    out.paste(img.crop((x0, src_y, x1, src_y + 44)), (x0, dst_y))
    return out


def test_pasted_row_is_found_with_both_regions(cfg):
    page = paste_row(make_page(), 80 + 2 * PITCH, 80 + 11 * PITCH)
    res = algo.copy_move_pairs(Image.open(io.BytesIO(to_jpeg(page))), cfg)
    assert len(res.regions) == 1
    pair = res.regions[0]
    assert pair.dy == 9 * PITCH and pair.coverage >= cfg.cmMinCoverage
    x, y, w, h = pair.second  # tight around the duplicated ink, inside the pasted band, covering most of its width
    assert 40 <= x and x + w <= 1060 and 80 + 11 * PITCH <= y and y + h <= 80 + 11 * PITCH + 44 and w > 800


def test_clean_page_has_no_copy_move(cfg):
    assert algo.copy_move_pairs(Image.open(io.BytesIO(to_jpeg(make_page()))), cfg).regions == []


def test_one_repeated_word_in_a_differing_line_is_not_flagged(cfg):
    """A merchant name repeated on two rows (the rest of the row differs) is ordinary content."""
    page = make_page(cols=4)
    word = page.crop((60, 80 + 3 * PITCH, 60 + 200, 80 + 3 * PITCH + 44))
    page.paste(word, (60, 80 + 12 * PITCH))
    assert algo.copy_move_pairs(Image.open(io.BytesIO(to_jpeg(page))), cfg).regions == []


def test_regular_pattern_of_identical_rows_is_ignored(cfg):
    page = make_page()
    src = 80 + 2 * PITCH
    for k in (6, 10, 14):  # four identical rows, equally spaced: a pattern, not a single paste
        page = paste_row(page, src, 80 + k * PITCH)
    assert algo.copy_move_pairs(Image.open(io.BytesIO(to_jpeg(page))), cfg).regions == []


def test_tiny_paste_is_ignored(cfg):
    page = make_page()
    page.paste(page.crop((60, 80, 90, 124)), (700, 900))   # one glyph
    assert algo.copy_move_pairs(Image.open(io.BytesIO(to_jpeg(page))), cfg).regions == []


def test_blank_page_is_skipped_not_clean(cfg):
    res = algo.copy_move_pairs(Image.new("RGB", (800, 800), "white"), cfg)
    assert res.skipped and not res.regions


# ------------------------------------------------------------------------------------------ noise
def test_flat_fill_over_paper_is_reported_as_quieter(cfg):
    page = make_page()
    ImageDraw.Draw(page).rectangle((200, 1250, 760, 1380), fill=(238, 238, 238))  # noise-free patch of blank paper
    res = algo.low_noise_regions(Image.open(io.BytesIO(to_jpeg(page, 90))), cfg)
    assert res.regions and iou(res.regions[0].box, (200, 1250, 560, 130)) > 0.3


def test_untouched_noisy_page_has_no_noise_signal(cfg):
    assert algo.low_noise_regions(Image.open(io.BytesIO(to_jpeg(make_page(), 90))), cfg).regions == []


def test_clean_digital_page_is_not_testable(cfg):
    res = algo.low_noise_regions(make_page(noise=0.0), cfg)
    assert res.skipped and "no measurable scan noise" in res.skipped


# ------------------------------------------------------------------------------------------ analyzers
def test_copymove_analyzer_emits_one_signal_with_two_labelled_regions(analyze):
    page = paste_row(make_page(), 80 + 2 * PITCH, 80 + 11 * PITCH)
    out = analyze("IMG-CM", to_jpeg(page), "jpg")
    assert out.statuses == {"image.copymove": "AVAILABLE", "image.ela": "AVAILABLE", "image.noise": "AVAILABLE"}
    sig = [s for s in out.signals if s.detector == "image.copymove"]
    assert len(sig) == 1 and sig[0].type == "COPY_MOVE" and len(sig[0].regions) == 2
    assert {r.label for r in sig[0].regions} == {"matching region A", "matching region B"}
    assert all(0 <= v <= 1 for r in sig[0].regions for v in r.bbox) and 0 < sig[0].confidence < 1
    assert sig[0].provenance.config["cfg.cmMinCoverage"] == 0.95 and sig[0].provenance.analyzer == "image.copymove"
    assert [s for s in out.signals if s.detector != "image.copymove"] == []


def test_clean_jpeg_gives_available_with_no_signals(analyze):
    out = analyze("IMG-OK", to_jpeg(make_page(), 90), "jpg")
    assert out.signals == [] and set(out.statuses.values()) == {"AVAILABLE"}


def test_png_original_has_no_ela(analyze):
    buf = io.BytesIO()
    make_page().save(buf, "PNG")
    out = analyze("IMG-PNG", buf.getvalue(), "png")
    assert out.statuses["image.ela"] == "NOT_APPLICABLE"
    assert out.statuses["image.copymove"] == "AVAILABLE"
    ela = next(p for p in out.provenance if p.analyzer == "image.ela")
    assert "JPEG" in ela.config["reason"]


def test_clean_digital_image_noise_is_not_applicable_never_clean(analyze):
    buf = io.BytesIO()
    make_page(noise=0.0).save(buf, "PNG")
    out = analyze("IMG-DIGI", buf.getvalue(), "png")
    assert out.statuses["image.noise"] == "NOT_APPLICABLE"
    noise = next(p for p in out.provenance if p.analyzer == "image.noise")
    assert "scan noise" in noise.config["reason"]


def test_digital_pdf_is_not_applicable(analyze):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 72), "FICTIONAL DIGITAL PDF with real text layer words")
    out = analyze("PDF-DIGI", doc.tobytes(), "pdf")
    assert set(out.statuses.values()) == {"NOT_APPLICABLE"} and out.signals == []


def test_scanned_pdf_page_runs_noise_and_copymove_but_not_ela(analyze):
    page = paste_row(make_page(), 80 + 2 * PITCH, 80 + 11 * PITCH)
    doc = pymupdf.open()
    pg = doc.new_page(width=550, height=750)
    pg.insert_image(pg.rect, stream=to_jpeg(page, 90))   # image only: no text layer
    out = analyze("PDF-SCAN", doc.tobytes(), "pdf")
    assert out.statuses["image.ela"] == "NOT_APPLICABLE"
    assert out.statuses["image.copymove"] == "AVAILABLE"
    assert any(s.detector == "image.copymove" for s in out.signals)


def test_exif_rotated_jpeg_skips_ela(analyze):
    img = make_page()
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif)
    out = analyze("IMG-ROT", buf.getvalue(), "jpg")
    assert out.statuses["image.ela"] == "NOT_APPLICABLE"


def test_large_image_is_analysed_at_working_size_and_boxes_stay_fractions(cfg):
    big = Image.new("RGB", (5200, 3000), "white")
    small = algo.to_working(big, cfg)
    assert max(small.size) == cfg.maxWorkingSidePx and abs(small.size[0] / small.size[1] - 5200 / 3000) < 0.01


def test_analyze_route_returns_valid_response_with_all_three(settings, put_original, make_client):
    page = paste_row(make_page(), 80 + 2 * PITCH, 80 + 11 * PITCH)
    put_original("IMG-API", "original.jpg", to_jpeg(page))
    reg = AnalyzerRegistry()
    for a in (ImageElaAnalyzer, ImageNoiseAnalyzer, ImageCopyMoveAnalyzer):
        reg.register(a)
    resp = make_client(reg).post("/analyze", json={"docId": "IMG-API"})
    assert resp.status_code == 200
    body = AnalyzeResponse.model_validate(resp.json())
    assert [s.type for s in body.signals] == ["COPY_MOVE"]
    assert all(s.detector in body.analyzerStatuses for s in body.signals)
    assert all(r.label for s in body.signals for r in s.regions)


def test_broken_config_is_an_error_not_a_clean_result(analyze, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    monkeypatch.setenv("IMAGE_FORENSICS_CONFIG", str(bad))
    out = analyze("IMG-BAD", to_jpeg(make_page()), "jpg")
    assert set(out.statuses.values()) == {"ERROR"} and out.signals == []
