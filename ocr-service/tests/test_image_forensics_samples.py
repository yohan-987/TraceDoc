"""O8 against the repo's D2 samples (skipped when the samples folder is absent).

Recorded results, not aspirations: the retyped-digits edit and the swapped photo are NOT detected today
(xfail below). Do not loosen thresholds to make them pass; see README "Image forensics".
"""
import json
from pathlib import Path

import pytest
from PIL import Image

from app import image_forensics as algo
from app.image_forensics_config import load_image_forensics_config

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
pytestmark = pytest.mark.skipif(not (SAMPLES / "tampered" / "ground_truth.json").exists(), reason="samples/ not present")

CFG = load_image_forensics_config()


def entries():
    return {e["id"]: e for e in json.loads((SAMPLES / "tampered" / "ground_truth.json").read_text())["entries"]}


def px_box(bbox, size):
    return (bbox[0] * size[0], bbox[1] * size[1], bbox[2] * size[0], bbox[3] * size[1])


def iou(a, b):
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def load(entry):
    img = algo.to_working(Image.open(SAMPLES / entry["file"]).convert("RGB"), CFG)
    return img


def test_pasted_statement_row_is_found():
    e = entries()["T-04"]
    img = load(e)
    pairs = algo.copy_move_pairs(img, CFG).regions
    assert len(pairs) == 1
    truth = px_box(e["bbox"], img.size)
    source = px_box(e["source_region"]["bbox"], img.size)
    assert max(iou(pairs[0].first, truth), iou(pairs[0].second, truth)) > 0.5
    assert max(iou(pairs[0].first, source), iou(pairs[0].second, source)) > 0.5  # both ends of the copy are reported


def test_replaced_signature_area_is_reported_as_quieter_than_the_page():
    e = entries()["T-05"]
    img = load(e)
    regions = algo.low_noise_regions(img, CFG).regions
    truth = px_box(e["bbox"], img.size)
    assert regions and max(iou(r.box, truth) for r in regions) > 0.3


UNTOUCHED = sorted(p for d in ("genuine", "controls") for ext in ("*.jpg", "*.png") for p in (SAMPLES / d).glob(ext))


@pytest.mark.parametrize("path", UNTOUCHED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_signal_on_untouched_documents_and_recompression_controls(path):
    img = algo.to_working(Image.open(path).convert("RGB"), CFG)
    assert algo.copy_move_pairs(img, CFG).regions == []
    assert algo.low_noise_regions(img, CFG).regions == []
    if path.suffix == ".jpg":
        assert algo.ela_regions(img, CFG).regions == []


@pytest.mark.xfail(reason="retyped digits were saved with the same JPEG history and scan texture: not detected", strict=False)
def test_retyped_digits_region_is_found():
    e = entries()["T-03"]
    img = load(e)
    truth = px_box(e["bbox"], img.size)
    boxes = [r.box for r in algo.low_noise_regions(img, CFG).regions] + [r.box for r in algo.ela_regions(img, CFG).regions]
    assert any(iou(b, truth) > 0.3 for b in boxes)


@pytest.mark.xfail(reason="the swapped photo is lossless PNG with no scan noise: not detected", strict=False)
def test_swapped_photo_region_is_found():
    e = entries()["T-06"]
    img = load(e)
    truth = px_box(e["bbox"], img.size)
    boxes = [r.box for r in algo.low_noise_regions(img, CFG).regions]
    assert any(iou(b, truth) > 0.3 for b in boxes)
