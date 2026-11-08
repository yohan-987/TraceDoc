"""Deterministic field extraction from one page of OCR words and lines (O4).

Strategies, all driven by the schema (app/schemas/*.yaml):
  anchor        a label regex, then the value after it on the same line; if the label stands alone, the value to its
                right on the same row, else on the line(s) below. An optional pattern narrows the value inside the text.
  composite     several labelled parts joined into one value (e.g. "Given names" + "Surname").
  anchor_region a box derived from a label's position (signature): a location only, no text is read.
Strategies are tried in schema order; the first that finds a candidate wins. Nothing is invented: a field with no
candidate is reported as missing, not returned.

extractionConfidence = the strategy's base confidence, adjusted for placement, pattern misses, competing candidates and the
expected region. It never uses OCR confidence, and OCR confidence never uses it. validationStatus is a format check only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .extract_normalize import is_ambiguous_numeric_date, normalize, resolve_pattern, validate
from .extract_schema import DocSchema, ExtractorSettings, FieldSpec, Strategy

BBox = Tuple[float, float, float, float]

_SEPARATORS = re.compile(r"^[\s:.\-–—_]+")
# A label-like word followed by a colon. ONE word only: a multi-word pattern would start at the last word(s) of the value
# before it ("John Smith Date of Birth:" must not be read as a label "Smith Date of Birth:").
_NEXT_LABEL = re.compile(r"(?<=\s)[A-Za-z][A-Za-z/'’.]*:(?=\s|$)")
_MAX_ANCHOR_LINES = 200   # anchor lines examined per strategy: bounds the work on pages with thousands of label-like lines
_LABEL_CUT_CACHE: Dict[Tuple[str, ...], "re.Pattern[str]"] = {}
_LABEL_START = re.compile(r"^\W*[A-Za-z][A-Za-z /'’.()]{1,38}\s*:")
_ALNUM = re.compile(r"[A-Za-z0-9]")


@dataclass(frozen=True)
class FieldResult:
    name: str
    semanticType: str
    rawValue: str
    normalizedValue: Optional[str]
    ocrConfidence: float
    extractionConfidence: float
    validationStatus: str
    bbox: BBox
    page: int
    strategy: str
    notes: Tuple[str, ...] = ()
    dateOrderAssumed: bool = False


@dataclass
class ExtractionOutcome:
    results: List[FieldResult] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Line:
    idx: int
    text: str
    spans: Tuple[Tuple[int, int, int], ...]  # (word index, start, end) within text
    bbox: BBox

    @property
    def x(self) -> float: return self.bbox[0]
    @property
    def y(self) -> float: return self.bbox[1]
    @property
    def w(self) -> float: return self.bbox[2]
    @property
    def h(self) -> float: return self.bbox[3]
    @property
    def yc(self) -> float: return self.bbox[1] + self.bbox[3] / 2


@dataclass
class _Cand:
    words: List[int]
    raw: str
    strategy: str
    placement: str = "same"           # same | right | below
    extra_lines: int = 0
    pattern_miss: bool = False
    distinct: int = 1
    anchor_line: int = 0
    region_box: Optional[BBox] = None  # anchor_region only


def _build_lines(page: Any) -> List[_Line]:
    n = len(page.words)
    lines = []
    for i, line in enumerate(page.lines):
        parts, spans, pos = [], [], 0
        for j in line.wordIndices:
            if not 0 <= j < n:
                continue
            text = page.words[j].text.strip()
            if not text:
                continue
            spans.append((j, pos, pos + len(text)))
            parts.append(text)
            pos += len(text) + 1
        if parts:
            lines.append(_Line(i, " ".join(parts), tuple(spans), tuple(line.bbox)))
    return lines


_ANCHOR_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _anchor_re(anchor: str) -> "re.Pattern[str]":
    rx = _ANCHOR_CACHE.get(anchor)
    if rx is None:
        rx = _ANCHOR_CACHE[anchor] = re.compile(rf"(?<![A-Za-z0-9])(?:{resolve_pattern(anchor)})(?![A-Za-z0-9])", re.I)
    return rx


def _find_anchor(line: _Line, anchors: Sequence[str]) -> Optional["re.Match[str]"]:
    """The most specific anchor on the line: the longest match wins (ties: earliest, then schema order)."""
    best = None
    for anchor in anchors:
        m = _anchor_re(anchor).search(line.text)
        if m and (best is None or (m.end() - m.start()) > (best.end() - best.start())):
            best = m
    return best


def _is_label_line(line: _Line, all_anchors: Sequence[str]) -> bool:
    if _LABEL_START.match(line.text):
        return True
    return any(re.match(r"^\W*", line.text) and _anchor_re(a).match(re.sub(r"^\W+", "", line.text)) for a in all_anchors)


def _label_cut_re(all_anchors: Sequence[str]) -> Optional["re.Pattern[str]"]:
    """Known labels of the schema (any field) followed by a colon, usable in the middle of a line."""
    key = tuple(a for a in all_anchors if not a.startswith("^"))  # '^' anchors only make sense at the line start
    if not key:
        return None
    rx = _LABEL_CUT_CACHE.get(key)
    if rx is None:
        body = "|".join(f"(?:{resolve_pattern(a)})" for a in key)
        rx = _LABEL_CUT_CACHE[key] = re.compile(rf"(?<=\s)(?<![A-Za-z0-9])(?:{body})\s*:(?=\s|$)", re.I)
    return rx


def _clean_remainder(text: str, start: int, all_anchors: Sequence[str] = ()) -> Tuple[int, int]:
    """Span of the value text after an anchor: separators skipped, cut before the next 'Label:' on the line.

    The next label is the earliest of (a) a known schema label followed by a colon, which may be several words
    ("Date of Birth:"), and (b) any single label-like word followed by a colon."""
    rest = text[start:]
    start += len(_SEPARATORS.match(rest).group(0)) if _SEPARATORS.match(rest) else 0
    rest = text[start:]
    cuts = [m.start() for m in (_NEXT_LABEL.search(rest),
                                (_label_cut_re(all_anchors).search(rest) if all_anchors and _label_cut_re(all_anchors) else None)) if m]
    end = start + (min(cuts) if cuts else len(rest))
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _has_content(text: str) -> bool:
    return bool(_ALNUM.search(text))


def _words_in_span(line: _Line, vs: int, ve: int) -> List[int]:
    return [j for j, s, e in line.spans if s < ve and e > vs]


def _narrow(text: str, vs: int, ve: int, strategy: Strategy) -> Tuple[int, int, bool, int]:
    """Apply the strategy pattern inside text[vs:ve] -> (start, end, pattern_miss, distinct matches)."""
    if not strategy.pattern:
        return vs, ve, False, 1
    rx = re.compile(resolve_pattern(strategy.pattern), re.I)
    found = [(vs + m.start(), vs + m.end()) for m in rx.finditer(text[vs:ve]) if m.group(0).strip()]
    if not found:
        return vs, ve, True, 1
    chosen = found[-1] if strategy.pick == "last" else found[0]
    cs, ce = chosen
    while text[cs].isspace():
        cs += 1
    distinct = len({text[a:b].strip().lower() for a, b in found})
    return cs, ce, False, distinct


def _right_neighbor(anchor: _Line, lines: Sequence[_Line]) -> Optional[_Line]:
    row = [l for l in lines if l.idx != anchor.idx and abs(l.yc - anchor.yc) <= 0.6 * max(l.h, anchor.h)
           and l.x >= anchor.x + anchor.w - 0.01]
    return min(row, key=lambda l: l.x) if row else None


def _line_below(prev: _Line, lines: Sequence[_Line], min_left: float, all_anchors: Sequence[str]) -> Optional[_Line]:
    below = [l for l in lines if l.idx != prev.idx and l.y >= prev.y + prev.h - 0.3 * prev.h
             and l.y - (prev.y + prev.h) <= 1.8 * prev.h and l.x >= min_left - 0.03]
    for line in sorted(below, key=lambda l: (l.y, l.x)):
        if abs(line.yc - prev.yc) <= 0.3 * prev.h:
            continue  # same row, not below
        if _is_label_line(line, all_anchors) or not _has_content(line.text):
            return None
        return line
    return None


def _value_from_line(line: _Line, vs: int, ve: int) -> Tuple[List[int], str]:
    return _words_in_span(line, vs, ve), line.text[vs:ve]


def _anchor_candidates(spec: FieldSpec, strat: Strategy, lines: Sequence[_Line], all_anchors: Sequence[str]) -> List[_Cand]:
    cands: List[_Cand] = []
    examined = 0
    for line in lines:
        m = _find_anchor(line, strat.anchors)
        if not m:
            continue
        examined += 1
        if examined > _MAX_ANCHOR_LINES:
            break
        vs, ve = _clean_remainder(line.text, m.end(), all_anchors)
        placement, value_lines, extra, anchor_x = "same", [line], 0, line.x
        if ve > vs and _has_content(line.text[vs:ve]):
            s, e, miss, distinct = _narrow(line.text, vs, ve, strat)
            words, raw = _value_from_line(line, s, e)
            last = line
        else:
            right = _right_neighbor(line, lines)
            target = None
            if right is not None:
                rs, re_ = _clean_remainder(right.text, 0, all_anchors)
                if re_ > rs and _has_content(right.text[rs:re_]) and not _is_label_line(right, all_anchors):
                    target, placement = (right, rs, re_), "right"
            if target is None:
                below = _line_below(line, lines, line.x, all_anchors)
                if below is None:
                    continue
                bs, be = _clean_remainder(below.text, 0, all_anchors)
                if not _has_content(below.text[bs:be]):
                    continue
                target, placement = (below, bs, be), "below"
            tline, ts, te = target
            s, e, miss, distinct = _narrow(tline.text, ts, te, strat)
            words, raw = _value_from_line(tline, s, e)
            last = tline
        if not words or not raw.strip():
            continue
        cand = _Cand(words, raw, "anchor", placement, 0, miss, distinct, line.idx)
        # multi-line values (addresses): append following unlabeled lines
        prev = last
        for _ in range(spec.continuationLines):
            nxt = _line_below(prev, lines, line.x, all_anchors)
            if nxt is None:
                break
            ns, ne = _clean_remainder(nxt.text, 0, all_anchors)
            if ne <= ns:
                break
            w2, r2 = _value_from_line(nxt, ns, ne)
            cand.words += w2
            cand.raw += " " + r2
            cand.extra_lines += 1
            prev = nxt
        cands.append(cand)
    return cands


def _composite_candidates(spec: FieldSpec, strat: Strategy, lines: Sequence[_Line], all_anchors: Sequence[str]) -> List[_Cand]:
    words: List[int] = []
    raws: List[str] = []
    placement_rank = {"same": 0, "right": 1, "below": 2}
    placement, miss = "same", False
    for part in strat.parts:
        sub = Strategy("anchor", strat.confidence, anchors=part, pattern=strat.pattern, pick=strat.pick)
        found = _anchor_candidates(FieldSpec(spec.name, spec.semanticType, spec.normalizer, spec.validators, (sub,)),
                                   sub, lines, all_anchors)
        if not found:
            return []
        first = found[0]
        words += first.words
        raws.append(first.raw)
        miss = miss or first.pattern_miss
        if placement_rank[first.placement] > placement_rank[placement]:
            placement = first.placement
    return [_Cand(words, " ".join(raws), "composite", placement, 0, miss, 1, 0)]


def _region_candidates(strat: Strategy, lines: Sequence[_Line], page: Any) -> List[_Cand]:
    for line in lines:
        m = _find_anchor(line, strat.anchors)
        if m is None:
            continue
        label = _words_in_span(line, m.start(), m.end())
        ax, ay, aw, ah = _union([tuple(page.words[j].bbox) for j in label]) if label else line.bbox
        placement = "same"  # the box is placed as configured (not a fallback)
        side = strat.placement
        if side == "right" and 1.0 - (ax + aw + 0.01) < 0.05:
            side, placement = "below", "below"  # no room to the right of the label
        if side == "right":
            x, y = ax + aw + 0.01, ay + ah / 2 - strat.height / 2
        else:
            x, y = ax, ay + ah + 0.005
        x, y = max(0.0, x), max(0.0, y)
        w, h = min(strat.width, 1.0 - x), min(strat.height, 1.0 - y)
        if w <= 0 or h <= 0:
            continue
        return [_Cand([], "", "anchor_region", placement, 0, False, 1, line.idx, (x, y, w, h))]
    return []


def _union(boxes: Sequence[BBox]) -> BBox:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, min(1.0, x1) - x0, min(1.0, y1) - y0)


def _inside(box: BBox, region: Tuple[float, float, float, float]) -> bool:
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    return region[0] <= cx <= region[0] + region[2] and region[1] <= cy <= region[1] + region[3]


def _score(base: float, cand: _Cand, spec: FieldSpec, box: BBox, settings: ExtractorSettings) -> float:
    sc = settings.scoring
    conf = base
    if cand.placement == "right":
        conf -= sc["rightPlacementPenalty"]
    elif cand.placement == "below":
        conf -= sc["belowPlacementPenalty"]
    if cand.pattern_miss:
        conf -= sc["patternMissPenalty"]
    if cand.distinct > 1:
        conf -= sc["multipleCandidatesPenalty"]
    conf -= sc["continuationPenalty"] * cand.extra_lines
    if spec.region is not None:
        conf += sc["regionMatchBonus"] if _inside(box, spec.region) else -sc["regionMissPenalty"]
    return round(min(1.0, max(0.0, conf)), 4)


def _extract_field(page: Any, page_no: int, spec: FieldSpec, lines: Sequence[_Line], all_anchors: Sequence[str],
                   settings: ExtractorSettings) -> Optional[FieldResult]:
    for strat in spec.strategies:
        if strat.kind == "anchor":
            cands = _anchor_candidates(spec, strat, lines, all_anchors)
        elif strat.kind == "composite":
            cands = _composite_candidates(spec, strat, lines, all_anchors)
        else:
            cands = _region_candidates(strat, lines, page)
        if not cands:
            continue

        def box_of(c: _Cand) -> BBox:
            return c.region_box if c.region_box else _union([tuple(page.words[j].bbox) for j in c.words])

        def rank(c: _Cand):
            inside = spec.region is None or _inside(box_of(c), spec.region)
            return (not inside, c.pattern_miss, c.placement != "same", c.anchor_line)

        best = sorted(cands, key=rank)[0]
        best.distinct = max(best.distinct, len({c.raw.strip().lower() for c in cands}))
        box = box_of(best)
        notes: List[str] = [f"strategy={best.strategy}", f"placement={best.placement}"]
        if best.pattern_miss:
            notes.append("text under the label did not match the expected pattern")
        if best.distinct > 1:
            notes.append(f"{best.distinct} different candidate values; first in reading order returned")
        if best.strategy == "anchor_region":
            notes.append("region observation only: no text was read and nothing is claimed about the content")
            return FieldResult(spec.name, spec.semanticType, "", None, 0.0, _score(strat.confidence, best, spec, box, settings),
                               validate(spec.validators, "", None), box, page_no, best.strategy, tuple(notes))
        normalized, assumed = normalize(spec.normalizer, best.raw, settings.dateOrder)
        if assumed:
            notes.append(f"numeric date read as {settings.dateOrder} (day and month both <= 12)")
        confs = [float(page.words[j].ocrConfidence) for j in best.words]
        ocr_conf = round(min(1.0, max(0.0, sum(confs) / len(confs))), 4)
        status = validate(spec.validators, best.raw, normalized)
        if spec.normalizer == "date" and normalized is None and is_ambiguous_numeric_date(best.raw):
            status = "NOT_CHECKED"  # well-formed but ambiguous, and no date order is configured
            notes.append("ambiguous numeric date left un-normalized: no dateOrder configured")
        return FieldResult(spec.name, spec.semanticType, best.raw, normalized, ocr_conf,
                           _score(strat.confidence, best, spec, box, settings), status, box, page_no,
                           best.strategy, tuple(notes), assumed)
    return None


def extract_fields(page: Any, schema: DocSchema, settings: ExtractorSettings) -> ExtractionOutcome:
    """Extract every field of ``schema`` from one OcrPage-like object. Missing fields are listed, never invented."""
    lines = _build_lines(page)
    all_anchors = [a for f in schema.fields for s in f.strategies for a in (s.anchors or tuple(x for p in s.parts for x in p))]
    out = ExtractionOutcome()
    for spec in schema.fields:
        result = _extract_field(page, page.page, spec, lines, all_anchors, settings)
        if result is None:
            out.missing.append(spec.name)
        else:
            out.results.append(result)
    return out
