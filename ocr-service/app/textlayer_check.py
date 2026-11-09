"""Text layer vs rendered page (O6): the comparison itself, free of PDF and OCR plumbing.

Inputs are two word lists in the same canonical-render coordinates (0..1, [x, y, w, h]): the PDF's own text layer and
OCR of the rendered page. A word that exists on one side only is a mismatch:

  * ``layer_only``   text is in the layer but nothing is visible there (white-on-white, invisible render mode, covered
                     by a patch, clipped). Decided with the PIXELS: the area must be blank, otherwise a word the OCR merely
                     failed to read would look hidden.
  * ``differs``      the layer says one thing and the page visibly shows another at the same place (stale or overlaid text).
  * ``render_only``  visible text that the layer does not contain (optional, noisy: logos and drawn text).

These are observations about where two readings of one page disagree. They do not say who changed what, or why.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image

from .models import OcrPage, OcrWord
from .textlayer_check_config import TextLayerCheckConfig

Box = Tuple[float, float, float, float]  # x, y, w, h, normalized
InkFn = Callable[[Box, Sequence[Box]], Optional[float]]  # ink share of a box outside the given boxes; None = cannot tell
RereadFn = Callable[[Box], Optional[str]]  # sharper second OCR reading of a box; None when it cannot be done

# OCR confuses these pairs all the time; both sides are folded the same way, so equal readings stay equal.
_FOLD = str.maketrans({"o": "0", "l": "1", "i": "1", "s": "5", "b": "8"})
_GRID = 48
_MAX_COMPARE_CHARS = 80
_MIN_FREE_SHARE = 0.7  # share of a layer word's box that must lie outside read words to judge it (a partial mask is not representative)


class TooManyWordsError(Exception):
    """A page has more words than the configured limit: comparing it would take unbounded time."""


@dataclass(frozen=True)
class Mismatch:
    kind: str                      # "layer_only" | "differs" | "render_only"
    layer_text: str
    render_text: str
    layer_box: Optional[Box]       # union of the layer words involved
    render_box: Optional[Box]      # union of the OCR words involved
    layer_words: int
    render_words: int
    reason: str = "nothing visible"    # layer_only: "nothing visible" (blank area) or "text too small to read"
    reread_text: Optional[str] = None  # second, sharper reading of the area (only for "differs")


@dataclass
class PageComparison:
    layer_words: int = 0           # eligible layer words compared
    ocr_words: int = 0             # eligible OCR words compared
    matched_layer_words: int = 0
    ignored_layer_words: int = 0   # too short, or too small to judge
    unread_ink: int = 0            # layer words with no OCR reading but visible ink: OCR miss, not judged
    agreement: float = 1.0
    page_level: bool = False       # OCR read plenty, yet the layer barely agrees with it
    not_comparable: bool = False   # OCR read too little of the page to judge the layer word by word
    mismatches: List[Mismatch] = field(default_factory=list)
    dropped: int = 0               # mismatches beyond the per-page cap
    reread_dropped: int = 0        # "differs" candidates the sharper second reading explained away (OCR misreads)
    unverified: int = 0            # "differs" candidates reported without a second reading


def normalize(text: str) -> str:
    """Letters and digits only, case-folded, with common OCR confusions folded together."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum()).translate(_FOLD)


def differing_chars(a: str, b: str) -> int:
    """How many letters/digits differ between two readings (insertions, deletions and substitutions together)."""
    plain = lambda s: "".join(ch for ch in unicodedata.normalize("NFKC", s).casefold() if ch.isalnum())  # noqa: E731
    pa, pb = plain(a), plain(b)
    same = sum(block.size for block in SequenceMatcher(None, pa[:_MAX_COMPARE_CHARS], pb[:_MAX_COMPARE_CHARS]).get_matching_blocks())
    return max(len(pa), len(pb)) - same


def _similarity(a: str, b: str) -> float:
    """1.0 for equal readings, else a difflib ratio."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a[:_MAX_COMPARE_CHARS], b[:_MAX_COMPARE_CHARS]).ratio()


def _matches(a: str, b: str, cfg: TextLayerCheckConfig, digits: bool) -> bool:
    """Do two normalized readings agree? ``digits``: a digit is involved (decided on the original text), so one wrong
    digit already means a different value and the stricter threshold applies."""
    return _similarity(a, b) >= (cfg.numericSimilarityMin if digits else cfg.textSimilarityMin)


def _contained(small: "_W", big: "_W") -> bool:
    """``small`` is one piece of ``big`` that the other reading merged (USD + 21,500.00 read as USD21,500.00).

    Only counts when the piece sits where that substring would be inside the merged word's box, so a value that merely
    happens to occur inside another value does not pass for it."""
    if len(small.norm) < 2 or len(small.norm) >= len(big.norm):
        return False
    idx = big.norm.find(small.norm)
    if idx < 0:
        return False
    expected = big.box[0] + big.box[2] * idx / len(big.norm)
    return abs(small.box[0] - expected) <= max(0.15 * big.box[2], 0.75 * small.box[2]) \
        and small.box[1] < big.box[1] + big.box[3] and big.box[1] < small.box[1] + small.box[3]


def _union(boxes: Sequence[Box]) -> Optional[Box]:
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def _area(b: Box) -> float:
    return max(0.0, b[2]) * max(0.0, b[3])


def _intersection(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


def _centre_in(point_box: Box, box: Box, pad_x: float, pad_y: float) -> bool:
    cx, cy = point_box[0] + point_box[2] / 2, point_box[1] + point_box[3] / 2
    return box[0] - pad_x <= cx <= box[0] + box[2] + pad_x and box[1] - pad_y <= cy <= box[1] + box[3] + pad_y


@dataclass
class _W:
    index: int
    text: str
    norm: str
    box: Box
    conf: float
    digits: bool = False   # the original text holds a digit (decided before OCR confusions are folded into digits)


class _Grid:
    """Uniform grid over the unit square, so overlap queries stay near-linear on dense pages."""

    def __init__(self, words: Sequence[_W], pad_x: float, pad_y: float) -> None:
        self.cells: Dict[Tuple[int, int], List[int]] = {}
        self.pad = (pad_x, pad_y)
        for w in words:
            for cell in self._cells(w.box):
                self.cells.setdefault(cell, []).append(w.index)

    def _cells(self, box: Box):
        px, py = self.pad
        x0 = max(0, int((box[0] - px) * _GRID)); x1 = min(_GRID - 1, int((box[0] + box[2] + px) * _GRID))
        y0 = max(0, int((box[1] - py) * _GRID)); y1 = min(_GRID - 1, int((box[1] + box[3] + py) * _GRID))
        return ((cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1))

    def near(self, box: Box) -> Set[int]:
        found: Set[int] = set()
        for cell in self._cells(box):
            found.update(self.cells.get(cell, ()))
        return found


def _eligible(words: Sequence[OcrWord], cfg: TextLayerCheckConfig, min_conf: float, height_px: int,
              min_height_px: int) -> Tuple[List[_W], int]:
    out: List[_W] = []
    ignored = 0
    for w in words:
        norm = normalize(w.text)
        if len(norm) < cfg.minWordChars or w.ocrConfidence < min_conf:
            ignored += 1
            continue
        box = tuple(float(v) for v in w.bbox)
        if not all(math.isfinite(v) for v in box) or box[2] <= 0 or box[3] <= 0 or box[3] * height_px < min_height_px:
            ignored += 1
            continue
        m = cfg.edgeMarginFrac
        if box[0] < m or box[1] < m or box[0] + box[2] > 1 - m or box[1] + box[3] > 1 - m:
            ignored += 1  # touches the page edge: the glyphs may be clipped, so neither reading is reliable
            continue
        out.append(_W(len(out), w.text, norm, box, float(w.ocrConfidence), any(ch.isdigit() for ch in w.text)))  # type: ignore[arg-type]
    return out, ignored


def _related(a: Box, b: Box, pad_x: float, pad_y: float) -> bool:
    inter = _intersection(a, b)
    if inter > 0 and inter >= 0.5 * min(_area(a), _area(b)):
        return True
    return _centre_in(a, b, pad_x, pad_y) or _centre_in(b, a, pad_x, pad_y)


def compare_page(layer: OcrPage, ocr: OcrPage, ink: InkFn, cfg: TextLayerCheckConfig,
                 reread: Optional[RereadFn] = None) -> PageComparison:
    if len(layer.words) > cfg.maxWordsPerPage or len(ocr.words) > cfg.maxWordsPerPage:
        raise TooManyWordsError(f"more than {cfg.maxWordsPerPage} words on the page")
    height_px, width_px = max(1, layer.heightPx), max(1, layer.widthPx)
    aspect = height_px / width_px
    layer_w, ignored = _eligible(layer.words, cfg, 0.0, height_px, cfg.minBoxHeightPx)
    ocr_w, _ = _eligible(ocr.words, cfg, cfg.minOcrConfidence, height_px, 2)
    result = PageComparison(layer_words=len(layer_w), ocr_words=len(ocr_w), ignored_layer_words=ignored)
    if not layer_w:
        return result

    def pads(box: Box) -> Tuple[float, float]:
        py = cfg.candidatePadFrac * box[3]
        return py * aspect, py

    max_h = max((w.box[3] for w in layer_w + ocr_w), default=0.0)
    ocr_grid = _Grid(ocr_w, cfg.candidatePadFrac * max_h * aspect, cfg.candidatePadFrac * max_h)
    layer_grid = _Grid(layer_w, cfg.candidatePadFrac * max_h * aspect, cfg.candidatePadFrac * max_h)

    # candidate relations, both directions
    t_cands: Dict[int, List[int]] = {}
    for t in layer_w:
        px, py = pads(t.box)
        t_cands[t.index] = sorted((i for i in ocr_grid.near(t.box) if _related(t.box, ocr_w[i].box, px, py)),
                                  key=lambda i: ocr_w[i].box[0])
    o_cands: Dict[int, List[int]] = {}
    for o in ocr_w:
        px, py = pads(o.box)
        o_cands[o.index] = sorted((i for i in layer_grid.near(o.box) if _related(o.box, layer_w[i].box, px, py)),
                                  key=lambda i: layer_w[i].box[0])

    def reading_matches(word: _W, others: List[_W]) -> bool:
        if not others:
            return False
        joined = "".join(o.norm for o in others)
        digits = word.digits or any(o.digits for o in others)
        return (_matches(word.norm, joined, cfg, digits)
                or any(_matches(word.norm, o.norm, cfg, word.digits or o.digits) or _contained(word, o) or _contained(o, word)
                       for o in others))

    def strong(t: _W, o: _W) -> bool:
        """Really the same place (not just touching): enough of the layer word is under the OCR word and enough of the OCR
        word is over the layer word. An OCR box inflated by a highlight or a neighbour fails the second test."""
        inter = _intersection(t.box, o.box)
        return inter >= cfg.differsMinOverlap * _area(t.box) and inter >= cfg.differsMinOcrShare * _area(o.box)

    t_strong = {t.index: [i for i in t_cands[t.index] if strong(t, ocr_w[i])] for t in layer_w}
    t_ok = {t.index: reading_matches(t, [ocr_w[i] for i in t_cands[t.index]]) for t in layer_w}
    o_ok = {o.index: reading_matches(o, [layer_w[i] for i in o_cands[o.index]]) for o in ocr_w}
    result.matched_layer_words = sum(t_ok.values())
    result.agreement = result.matched_layer_words / len(layer_w)
    # Too little agreement means the two readings cannot be compared word by word (a page OCR cannot read, or a layer that
    # has little to do with the image). Words whose area is plainly blank are still reported one by one; "differs" and
    # "render_only" need a trustworthy OCR reading and are skipped. The page as a whole is reported as well.
    enough_words = len(layer_w) >= cfg.minWordsForAgreement
    result.not_comparable = enough_words and len(ocr_w) < cfg.minOcrCoverage * len(layer_w)
    result.page_level = enough_words and not result.not_comparable and result.agreement < cfg.minPageAgreement
    degraded = result.page_level or result.not_comparable  # only blank-area words can be judged on such a page

    # nodes: unmatched layer words (blank area, or text that differs) and unmatched OCR words
    nodes: Dict[Tuple[str, int], str] = {}
    tiny: Set[int] = set()
    for t in layer_w:
        if t_ok[t.index]:
            continue
        if t.box[3] * height_px < cfg.tinyHeightPx and not t_strong[t.index]:
            nodes[("t", t.index)] = "layer_only"  # no reading, and too small for anyone to read it
            tiny.add(t.index)
        elif not t_strong[t.index]:
            # pixels that belong to words OCR did read (a neighbour, a line above) are not evidence of visible text here
            share = ink(t.box, [ocr_w[i].box for i in ocr_grid.near(t.box) if _intersection(t.box, ocr_w[i].box) > 0])
            if share is not None and share < cfg.inkMinFraction:
                nodes[("t", t.index)] = "layer_only"
            else:
                result.unread_ink += 1
        elif not degraded:
            nodes[("t", t.index)] = "differs"
    for o in ocr_w:
        if o_ok[o.index] or degraded:
            continue
        linked = [i for i in o_cands[o.index] if ("t", i) in nodes and i in t_strong and o.index in t_strong[i]]
        if linked:
            nodes[("o", o.index)] = "differs"
        elif not o_cands[o.index] and cfg.flagRenderOnly:
            nodes[("o", o.index)] = "render_only"

    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (side, i), kind in nodes.items():
        if side == "o" and kind == "differs":
            for j in o_cands[i]:
                if ("t", j) in nodes and i in t_strong[j]:
                    union(("o", i), ("t", j))
        if side == "t" and kind == "differs":
            for j in t_strong[i]:
                if ("o", j) in nodes:
                    union(("t", i), ("o", j))

    groups: Dict[Tuple[str, int], List[Tuple[str, int]]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)

    comps = []
    for members in groups.values():
        ts = sorted((layer_w[i] for s, i in members if s == "t"), key=lambda w: w.index)
        os_ = sorted((ocr_w[i] for s, i in members if s == "o"), key=lambda w: w.index)
        if ts and os_:
            kind = "differs"
        elif ts and all(nodes[("t", w.index)] == "layer_only" for w in ts):
            kind = "layer_only"
        elif ts:
            # OCR covers these words but its words are explained by other layer words (a merged token such as 16Feb):
            # something is visible there, so they are not hidden
            result.unread_ink += len(ts)
            continue
        else:
            kind = "render_only"
        comps.append({"kind": kind, "t": ts, "o": os_, "reread": []})

    # a "differs" cluster is only a finding if a second, sharper reading of THAT cluster agrees with the first one:
    # small text is often misread (1 for 4), and verifying one word at a time keeps a stray digit from keeping a column alive
    verified = []
    rereads = 0
    for c in comps:
        if c["kind"] == "differs":
            if reread is None or rereads >= cfg.maxRereadsPerPage:
                result.unverified += 1
            else:
                rereads += 1
                box = _union([w.box for w in c["t"]] + [w.box for w in c["o"]])
                text = reread(box) if box else None
                if text is None:
                    result.unverified += 1
                elif _explains(c, text, cfg):
                    result.reread_dropped += 1  # the sharper reading agrees with the layer: the first OCR reading was wrong
                    continue
                else:
                    c["reread"].append(text)
        verified.append(c)
    comps = verified

    def comp_box(c) -> Box:
        return _union([w.box for w in c["t"]] + [w.box for w in c["o"]])  # type: ignore[return-value]

    # neighbouring mismatches of one kind on one line are one phrase ("USD" + "56,500.00")
    comps.sort(key=lambda c: (comp_box(c)[1], comp_box(c)[0]))
    merged: List[dict] = []
    for c in comps:
        box = comp_box(c)
        target = None
        for m in reversed(merged[-8:]):
            mb = comp_box(m)
            same_line = min(mb[1] + mb[3], box[1] + box[3]) - max(mb[1], box[1]) >= 0.5 * min(mb[3], box[3])
            gap = box[0] - (mb[0] + mb[2])
            beside = same_line and gap <= cfg.groupGapFactor * max(mb[3], box[3]) * aspect and gap >= -mb[2]
            # stacked: same column and close in height (the next line of a block, or the next word of vertical text)
            same_column = min(mb[0] + mb[2], box[0] + box[2]) - max(mb[0], box[0]) >= 0.5 * min(mb[2], box[2])
            vgap = box[1] - (mb[1] + mb[3])
            below = same_column and -mb[3] <= vgap <= cfg.groupGapFactor * max(mb[3], box[3])
            if m["kind"] == c["kind"] and (beside or below):
                target = m
                break
        if target is None:
            merged.append({"kind": c["kind"], "t": list(c["t"]), "o": list(c["o"]), "reread": list(c["reread"])})
        else:
            target["t"] += c["t"]
            target["o"] += c["o"]
            target["reread"] += c["reread"]
            target["t"].sort(key=lambda w: w.index)
            target["o"].sort(key=lambda w: w.index)

    found: List[Mismatch] = []
    for c in merged:
        layer_text = " ".join(w.text for w in c["t"])
        render_text = " ".join(w.text for w in c["o"])
        chars = len(normalize(layer_text if c["kind"] != "render_only" else render_text))
        if chars < cfg.minGroupChars:
            continue  # a tiny difference
        reason = "text too small to read" if c["kind"] == "layer_only" and all(w.index in tiny for w in c["t"]) else "nothing visible"
        found.append(Mismatch(c["kind"], layer_text, render_text, _union([w.box for w in c["t"]]),
                              _union([w.box for w in c["o"]]), len(c["t"]), len(c["o"]), reason=reason,
                              reread_text=" ".join(c["reread"]) if c["reread"] else None))
    found.sort(key=lambda m: ((m.layer_box or m.render_box)[1], (m.layer_box or m.render_box)[0]))  # type: ignore[index]
    result.dropped = max(0, len(found) - cfg.maxSignalsPerPage)
    result.mismatches = found[:cfg.maxSignalsPerPage]
    return result


def make_ink_fn(image: Image.Image, cfg: TextLayerCheckConfig) -> InkFn:
    """Share of pixels in a box that differ from the box's own background by more than ``inkDelta``.

    Pixels inside ``exclude`` boxes (words OCR read) are left out. When less than ``_MIN_FREE_SHARE`` of the box remains the
    answer is None: too little is left to say whether anything is visible there."""
    grey = np.asarray(image.convert("L"), dtype=np.int16)
    height, width = grey.shape

    def pixel_rect(b: Box, pad: int) -> Tuple[int, int, int, int]:
        return (max(0, int(math.floor(b[0] * width)) - pad), max(0, int(math.floor(b[1] * height)) - pad),
                min(width, int(math.ceil((b[0] + b[2]) * width)) + pad), min(height, int(math.ceil((b[1] + b[3]) * height)) + pad))

    def ink(box: Box, exclude: Sequence[Box] = ()) -> Optional[float]:
        x0, y0, x1, y1 = pixel_rect(box, cfg.inkPadPx)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = grey[y0:y1, x0:x1]
        free = np.ones(crop.shape, dtype=bool)
        for e in exclude:
            ex0, ey0, ex1, ey1 = pixel_rect(e, 1)
            free[max(0, ey0 - y0):max(0, ey1 - y0), max(0, ex0 - x0):max(0, ex1 - x0)] = False
        if free.sum() < _MIN_FREE_SHARE * free.size:
            return None
        values = crop[free]
        return float(np.mean(np.abs(values - np.median(values)) > cfg.inkDelta))

    return ink


def _explains(comp: dict, text: str, cfg: TextLayerCheckConfig) -> bool:
    """Does a sharper reading of the cluster agree with what the layer says?"""
    layer_text = " ".join(w.text for w in comp["t"])
    layer_norm, reading_norm = normalize(layer_text), normalize(text)
    digits = any(ch.isdigit() for ch in layer_text + text)
    return bool(layer_norm) and (layer_norm in reading_norm or any(_matches(layer_norm, normalize(w), cfg, digits) for w in text.split())
                                 or _matches(layer_norm, reading_norm, cfg, digits))
