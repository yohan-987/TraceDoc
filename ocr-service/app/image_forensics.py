"""Image-forensics algorithms (O8): error-level analysis, low-noise regions and copy-move.

Pure numpy/OpenCV/Pillow, no service types. Boxes are returned in pixels of the *working* image (the page
downscaled so its long side is at most ``maxWorkingSidePx``); the analyzer converts them to page fractions.

Every function reports an observation about pixels, never a conclusion about a person or intent:
  * ELA        - blocks whose JPEG re-save residual is unusual for the amount of edge detail they hold;
  * low noise  - paper-only blocks much quieter than the rest of the page (typical of a flat fill or an erased
                 area; also of a clean digital page, which is why a page with no noise at all is not tested);
  * copy-move  - two places holding the same ink pixels, found with ORB matches and then confirmed by comparing
                 pixels, so repeated words in a table do not count; regular patterns (also repeated at twice
                 the offset) are ignored.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from .image_forensics_config import ImageForensicsConfig

PixelBox = Tuple[int, int, int, int]  # x, y, w, h


@dataclass(frozen=True)
class Region:
    box: PixelBox
    score: float                       # detector-specific strength (documented per detector)
    measurements: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CopyMovePair:
    first: PixelBox
    second: PixelBox
    dx: int
    dy: int
    votes: int
    matched_ink_px: int
    coverage: float
    glyph_components: int


@dataclass(frozen=True)
class StageResult:
    """One detector's outcome. ``skipped`` carries the reason a test could not be run (not a clean result)."""

    regions: List = field(default_factory=list)
    skipped: Optional[str] = None
    stats: Dict[str, float] = field(default_factory=dict)


def working_size(width: int, height: int, cfg: ImageForensicsConfig) -> Tuple[int, int]:
    longest = max(width, height)
    if longest <= cfg.maxWorkingSidePx:
        return width, height
    scale = cfg.maxWorkingSidePx / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def to_working(img: Image.Image, cfg: ImageForensicsConfig) -> Image.Image:
    w, h = working_size(*img.size, cfg)
    if (w, h) == img.size:
        return img
    return img.resize((w, h), Image.LANCZOS)


def _blocks(arr: np.ndarray, b: int) -> np.ndarray:
    h, w = arr.shape[0] // b, arr.shape[1] // b
    return arr[: h * b, : w * b].reshape(h, b, w, b).mean(axis=(1, 3))


def _robust_z(values: np.ndarray) -> Tuple[np.ndarray, float, float]:
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    return (values - med) / (1.4826 * mad + 1e-6), med, mad


def _cluster(mask: np.ndarray, block: int, min_blocks: int, z: np.ndarray, sign: float) -> List[Region]:
    """Connected groups of marked blocks (8-neighbourhood) -> regions; score = strongest |z| in the group."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    out: List[Region] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < min_blocks:
            continue
        zs = z[labels == i] * sign
        out.append(Region((x * block, y * block, w * block, h * block), float(zs.max()),
                          {"blocks": float(area), "meanZ": float(zs.mean())}))
    return sorted(out, key=lambda r: -r.score)


# ------------------------------------------------------------------------------------------------ ELA
def ela_regions(img: Image.Image, cfg: ImageForensicsConfig) -> StageResult:
    """Re-save as JPEG, compare, and score each block's residual against the edge detail it holds.

    Plain ELA is dominated by text edges on a document page, so the residual is divided by the block's edge
    energy (fitted on the page's own detailed blocks) and scored with a robust z against the page itself.
    """
    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=cfg.elaQuality)
    buf.seek(0)
    resaved = np.asarray(Image.open(buf).convert("RGB"), dtype=np.float32)
    residual = np.abs(np.asarray(rgb, dtype=np.float32) - resaved).mean(axis=2)
    gray = np.asarray(rgb.convert("L"), dtype=np.float32)
    grad = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1))
    b = cfg.elaBlockPx
    res_b, grad_b = _blocks(residual, b), _blocks(grad, b)
    if res_b.size == 0 or not (grad_b > 0).any():
        return StageResult(skipped="image too small or flat for error-level analysis")
    active = grad_b > np.percentile(grad_b[grad_b > 0], cfg.elaActivePercentile)
    if int(active.sum()) < 20:
        return StageResult(skipped="too little detail for error-level analysis")
    scale = float(np.median(res_b[active] / np.maximum(grad_b[active], 1e-3)))
    ratio = np.zeros_like(res_b)
    ratio[active] = res_b[active] / np.maximum(grad_b[active] * scale, 1e-3)
    z = np.zeros_like(res_b)
    z[active], med, mad = _robust_z(ratio[active])
    regions = _cluster(z > cfg.elaZThreshold, b, cfg.elaMinBlocks, z, 1.0)
    return StageResult(regions=regions, stats={"activeBlocks": float(active.sum()), "ratioMedian": med,
                                               "ratioMad": mad, "maxZ": float(z.max())})


# ------------------------------------------------------------------------------------------------ noise
def low_noise_regions(img: Image.Image, cfg: ImageForensicsConfig) -> StageResult:
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    residual = gray - cv2.GaussianBlur(gray, (0, 0), cfg.noiseHighPassSigma)
    b = cfg.noiseBlockPx
    h, w = gray.shape[0] // b, gray.shape[1] // b
    if h < 4 or w < 4:
        return StageResult(skipped="image too small for a noise test")
    gb = gray[: h * b, : w * b].reshape(h, b, w, b)
    rb = residual[: h * b, : w * b].reshape(h, b, w, b)
    paper = gb.min(axis=(1, 3)) >= cfg.noiseBackgroundMinLevel
    sd = rb.std(axis=(1, 3))
    if int(paper.sum()) < cfg.noiseMinBackgroundBlocks:
        return StageResult(skipped="too little blank paper to measure the noise level")
    z_paper, med, mad = _robust_z(sd[paper])
    if med < cfg.noiseMinMedianSd or mad < cfg.noiseMinMad:
        return StageResult(skipped="page has no measurable scan noise (clean digital image): test not informative",
                           stats={"noiseSdMedian": med, "noiseSdMad": mad})
    z = np.zeros_like(sd)
    z[paper] = z_paper
    regions = _cluster(z < -cfg.noiseLowZThreshold, b, cfg.noiseMinBlocks, z, -1.0)
    return StageResult(regions=regions, stats={"paperBlocks": float(paper.sum()), "noiseSdMedian": med,
                                               "noiseSdMad": mad, "minZ": float(z.min())})


# ------------------------------------------------------------------------------------------------ copy-move
def _overlap(shape: Tuple[int, int], dx: int, dy: int):
    """Slices (a, b) of two equally sized views where b is a shifted by (dx, dy); None if no overlap."""
    height, width = shape
    if dy < 0 or abs(dx) >= width or dy >= height:
        return None
    ax0, ax1 = (0, width - dx) if dx >= 0 else (-dx, width)
    bx0, bx1 = (dx, width) if dx >= 0 else (0, width + dx)
    return (slice(0, height - dy), slice(ax0, ax1)), (slice(dy, height), slice(bx0, bx1)), ax0


def _offset_votes(gray: np.ndarray, cfg: ImageForensicsConfig) -> Tuple[List[Tuple[int, int, int]], int]:
    orb = cv2.ORB_create(nfeatures=cfg.cmOrbFeatures, scaleFactor=1.2, nlevels=6, edgeThreshold=15,
                         patchSize=31, fastThreshold=10)
    keypoints, desc = orb.detectAndCompute(gray, None)
    if desc is None or len(keypoints) < 20:
        return [], len(keypoints or [])
    pts = np.array([k.pt for k in keypoints], dtype=np.float32)
    votes: Dict[Tuple[int, int], int] = {}
    for row in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc, desc, k=3):
        cand = [m for m in row if m.trainIdx != m.queryIdx]
        if len(cand) < 2:
            continue
        best, second = cand[0], cand[1]
        if best.distance > cfg.cmMaxDescriptorDistance or best.distance > cfg.cmRatio * max(second.distance, 1):
            continue
        d = pts[best.trainIdx] - pts[best.queryIdx]
        if float(np.hypot(d[0], d[1])) < cfg.cmMinOffsetPx:
            continue
        if d[1] < 0 or (d[1] == 0 and d[0] < 0):
            d = -d  # one vote per unordered pair
        key = (round(float(d[0]) / cfg.cmOffsetBinPx), round(float(d[1]) / cfg.cmOffsetBinPx))
        votes[key] = votes.get(key, 0) + 1
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    out = [(k[0] * cfg.cmOffsetBinPx, k[1] * cfg.cmOffsetBinPx, v) for k, v in ranked
           if v >= cfg.cmMinVotes][: cfg.cmMaxCandidates]
    return out, len(keypoints)


def _same_ink(gray: np.ndarray, dx: int, dy: int, cfg: ImageForensicsConfig):
    """Ink pixels (in the upper/left view) that are also ink and equal in the view shifted by (dx, dy)."""
    sl = _overlap(gray.shape, dx, dy)
    if sl is None:
        return None
    (ay, ax), (by, bx), ax0 = sl
    a, b = gray[ay, ax].astype(np.int16), gray[by, bx].astype(np.int16)
    ink = np.minimum(a, b) < cfg.cmInkLevel
    same = (np.abs(a - b) < cfg.cmPixelTolerance) & ink
    return same, ink, ax0


def _refine(gray: np.ndarray, dx: int, dy: int, cfg: ImageForensicsConfig):
    best = None
    for ddx in range(-cfg.cmRefinePx, cfg.cmRefinePx + 1):
        for ddy in range(-cfg.cmRefinePx, cfg.cmRefinePx + 1):
            res = _same_ink(gray, dx + ddx, dy + ddy, cfg)
            if res is None:
                continue
            score = int(res[0].sum())
            if best is None or score > best[0]:
                best = (score, dx + ddx, dy + ddy, res)
    return best


def _boxes_overlap(a: PixelBox, b: PixelBox) -> bool:
    ix = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    iy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return ix > 0 and iy > 0


def _same_pair(first: PixelBox, second: PixelBox, other: "CopyMovePair") -> bool:
    """The same duplicate seen again (from another offset candidate, or with the two sides swapped)."""
    def near(a: PixelBox, b: PixelBox) -> bool:
        return abs(a[0] - b[0]) < 12 and abs(a[1] - b[1]) < 12
    return (near(first, other.first) and near(second, other.second)) or (near(first, other.second) and near(second, other.first))


def _is_periodic(gray: np.ndarray, first: PixelBox, second: PixelBox, matched: int, dx: int, dy: int,
                 cfg: ImageForensicsConfig) -> bool:
    """True when the same pixels also repeat one more step along the offset, forward or backward (3+ equal copies)."""
    res = _same_ink(gray, dx, dy, cfg)
    if res is None:
        return False
    same, _ink, ax0 = res
    for x, y, w, h in (second, (first[0] - dx, first[1] - dy, first[2], first[3])):
        x_in_a, y_in_a = x - ax0, y            # a box expressed in the first view's coordinates
        if x_in_a < 0 or y_in_a < 0 or y_in_a + h > same.shape[0] or x_in_a + w > same.shape[1]:
            continue
        if float(same[y_in_a:y_in_a + h, x_in_a:x_in_a + w].sum()) / max(matched, 1) >= cfg.cmPeriodicCoverage:
            return True
    return False


def copy_move_pairs(img: Image.Image, cfg: ImageForensicsConfig) -> StageResult:
    gray = np.asarray(img.convert("L"))
    height, width = gray.shape
    candidates, n_keypoints = _offset_votes(gray, cfg)
    stats = {"keypoints": float(n_keypoints), "candidateOffsets": float(len(candidates))}
    if n_keypoints < 20:
        return StageResult(skipped="too few image features for copy-move analysis", stats=stats)
    min_ink = cfg.cmMinMatchedInkFraction * width * height
    merge_w = max(3, int(cfg.cmMergeGapFraction * width)) | 1
    pairs: List[CopyMovePair] = []
    for ox, oy, votes in candidates:
        refined = _refine(gray, ox, oy, cfg)
        if refined is None:
            continue
        _, dx, dy, (same, ink, ax0) = refined
        merged = cv2.morphologyEx(same.astype(np.uint8), cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (merge_w, 9)))
        n, labels, st, _ = cv2.connectedComponentsWithStats(merged)
        bands_done: List[Tuple[int, int]] = []
        for i in sorted(range(1, n), key=lambda j: -int(st[j][cv2.CC_STAT_AREA])):
            y, h = int(st[i][cv2.CC_STAT_TOP]), int(st[i][cv2.CC_STAT_HEIGHT])
            if any(y < b1 and b0 < y + h for b0, b1 in bands_done):
                continue                                           # another piece of a text line already judged
            bands_done.append((y, y + h))
            # judge the whole text line (every row of the band), not one column of it
            band_same, band_ink = same[y:y + h, :], ink[y:y + h, :]
            matched = int(band_same.sum())
            if matched < min_ink:
                continue                                           # a tiny match
            cols = np.where(band_ink.any(axis=0))[0]
            line_ink = int(band_ink[:, cols.min():cols.max() + 1].sum()) if cols.size else 0
            coverage = matched / max(line_ink, 1)
            if coverage < cfg.cmMinCoverage:
                continue                                           # repeated words inside a line that differs
            same_cols = np.where(band_same.any(axis=0))[0]
            x, w = int(same_cols.min()), int(same_cols.max() - same_cols.min() + 1)
            glyphs = cv2.connectedComponents(band_same[:, x:x + w].astype(np.uint8))[0] - 1
            if glyphs < cfg.cmMinGlyphComponents:
                continue
            first = (x + ax0, y, w, h)
            second = (x + ax0 + dx, y + dy, w, h)
            if _boxes_overlap(first, second):
                continue                                           # a shifted copy of itself: layout, not a paste
            if _is_periodic(gray, first, second, matched, dx, dy, cfg):
                continue                                           # rule lines, identical rows: a regular pattern
            if any(_same_pair(first, second, q) for q in pairs):
                continue
            pairs.append(CopyMovePair(first, second, dx, dy, votes, matched, round(coverage, 3), int(glyphs)))
    # three or more equal copies: a regular pattern, not a single paste. A box that takes part in two pairs
    # (e.g. rows 1,2,3 give pairs 1-2, 1-3, 2-3) belongs to such a group, so every pair touching it is dropped.
    def uses(box: PixelBox, other: CopyMovePair) -> bool:
        return _same_pair(box, box, CopyMovePair(other.first, other.first, 0, 0, 0, 0, 0.0, 0)) or \
            _same_pair(box, box, CopyMovePair(other.second, other.second, 0, 0, 0, 0, 0.0, 0))
    repeated = [p for p in pairs if sum(uses(p.first, q) or uses(p.second, q) for q in pairs) >= 2]
    pairs = [p for p in pairs if p not in repeated]
    pairs.sort(key=lambda p: -p.matched_ink_px)
    return StageResult(regions=pairs, stats=stats)
