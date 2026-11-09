"""Deterministic field extraction from one page of OCR words and lines (O4).

Strategies, all driven by the schema (app/schemas/*.yaml):
  anchor        a label regex, then the value: after the label on the same line, to its right on the same row, or below it
                (label above value), whichever validated candidate is geometrically nearest. A candidate is never a printed
                label (the schema's anchors and `labels`, plus the shared `labelVocabulary`) nor a format hint such as
                "(YYYY-MM-DD)"; below a label the words are limited to the label's column (up to the next label on its row),
                using the OCR word boxes. An optional pattern narrows the value inside the text.
  composite     several labelled parts joined into one value (e.g. "Given names" + "Surname").
  anchor_region a box derived from a label's position (signature): a location only, no text is read.
Strategies are tried in schema order; the first that finds a candidate wins. Nothing is invented: a field with no
candidate is reported as missing, not returned.

extractionConfidence = the strategy's base confidence, adjusted for placement, pattern misses, competing candidates and the
expected region. It never uses OCR confidence, and OCR confidence never uses it. validationStatus is a format check only.
"""
from __future__ import annotations

import bisect
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
_LABEL_START = re.compile(r"^\W*[A-Za-z][A-Za-z /'’.()]{1,38}\s*:")
_ALNUM = re.compile(r"[A-Za-z0-9]")
# Format hints printed next to a label: "(YYYY-MM-DD)", "(optional)", "[as on ID]". Parenthesised text WITHOUT letters is not
# a hint (a negative amount "(1,250.00)" is a value).
_HINT_GROUP = re.compile(r"^\s*(?:\((?=[^()]*[A-Za-z])[^()]*\)|\[(?=[^\[\]]*[A-Za-z])[^\[\]]*\])\s*")
_FORMAT_TOKEN = re.compile(r"^\W*(?:y{2,4}|m{1,2}|d{1,2})(?:\s*[-/.]\s*(?:y{2,4}|m{1,2}|d{1,2})){1,2}\W*$", re.I)


def _is_hint(text: str) -> bool:
    """True when ``text`` is only a format hint (a parenthesised note or a date-format template)."""
    t = text.strip()
    if not t:
        return False
    if _FORMAT_TOKEN.match(t):
        return True
    m = _HINT_GROUP.match(t)
    return bool(m) and not t[m.end():].strip()


def _skip_hints(text: str, start: int) -> int:
    """Index after any format hints that follow position ``start`` ("Date of birth (YYYY-MM-DD) 1983-09-18")."""
    while True:
        m = _HINT_GROUP.match(text[start:])
        if not m:
            return start
        start += m.end()


_CONFUSABLE = {"i": "[il1|]", "l": "[li1|]", "o": "[o0]"}


def _ocr_tolerant(pattern: str) -> str:
    """The label regex with plain letters i, l, o made to accept their usual OCR look-alikes (1, |, 0): a scan reads
    "ISSUED" as "1SSUED" and "ID number" as "1D number". Escapes, character classes and group prefixes are left alone.
    Used only to RECOGNISE labels (so they are not taken for values), never to find a field's own anchor."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            out.append(pattern[i:i + 2]); i += 2
        elif c == "[":
            j = pattern.find("]", i + 1)
            j = n - 1 if j < 0 else j
            out.append(pattern[i:j + 1]); i = j + 1
        elif c == "(" and pattern[i:i + 2] == "(?":
            k = i + 2
            if pattern[k:k + 1] == "<":
                k += 1
            out.append(pattern[i:k + 1]); i = k + 1
        else:
            out.append(_CONFUSABLE.get(c.lower(), c)); i += 1
    return "".join(out)


class _Vocab:
    """The printed labels of a page's document type: every schema anchor, the schema's `labels` and the shared vocabulary.

    ``spans`` finds label occurrences inside a text (the longest wins where they overlap); ``is_label`` tells whether a
    text is made of nothing but labels (so it can never be a field value)."""

    def __init__(self, patterns: Sequence[str]) -> None:
        seen, self._pats, self._whole = set(), [], []
        for p in patterns:
            if p in seen:
                continue
            seen.add(p)
            body = _ocr_tolerant(resolve_pattern(p))
            self._whole.append(re.compile(rf"^\W*(?:{body})\W*$", re.I))
            if not p.startswith("^"):           # '^' anchors only make sense at the start of a line
                self._pats.append(re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.I))

    def spans(self, text: str) -> List[Tuple[int, int]]:
        found = sorted({(m.start(), m.end()) for rx in self._pats for m in rx.finditer(text) if m.end() > m.start()},
                       key=lambda se: (se[0], -(se[1] - se[0])))
        out: List[Tuple[int, int]] = []
        for s_, e_ in found:
            if not out or s_ >= out[-1][1]:
                out.append((s_, e_))
        return out

    def is_label(self, text: str) -> bool:
        t = text.strip()
        if not _ALNUM.search(t):
            return False
        if any(rx.match(t) for rx in self._whole):
            return True
        rest, last = [], 0
        for s_, e_ in self.spans(t):
            rest.append(t[last:s_])
            last = e_
        rest.append(t[last:])
        return last > 0 and not _ALNUM.search("".join(rest))


_VOCAB_CACHE: Dict[Tuple[str, ...], _Vocab] = {}


def _vocab(patterns: Sequence[str]) -> _Vocab:
    key = tuple(patterns)
    v = _VOCAB_CACHE.get(key)
    if v is None:
        if len(_VOCAB_CACHE) > 64:
            _VOCAB_CACHE.clear()
        v = _VOCAB_CACHE[key] = _Vocab(patterns)
    return v


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
    valid: bool = True                 # the value validates for the field type (date parses, ID format, amount, name ...)
    anchor_pos: Tuple[float, float] = (0.0, 0.0)  # (y, x) of the label: reading order by geometry, not by OCR line order
    last_line: Optional[int] = None    # position in `lines` of the line the value ended on (continuation lines start here)


@dataclass
class _Page:
    """What the geometric association needs about one page: OCR words, lines, label vocabulary and settings."""
    ocr: Any
    lines: List[_Line]
    vocab: _Vocab
    settings: ExtractorSettings
    word_line: Dict[int, int]          # word index -> position in `lines`
    # vertical indexes so each label only examines the words / lines in its own band (pages can have thousands of lines)
    word_order: List[int] = field(default_factory=list)       # word indexes sorted by top edge
    word_tops: List[float] = field(default_factory=list)
    max_word_h: float = 0.0
    line_order: List[int] = field(default_factory=list)       # positions in `lines` sorted by vertical centre
    line_centres: List[float] = field(default_factory=list)
    max_line_h: float = 0.0

    def __post_init__(self) -> None:
        boxes = [(tuple(w.bbox), j) for j, w in enumerate(self.ocr.words) if w.text.strip()]
        boxes.sort(key=lambda t: t[0][1])
        self.word_order = [j for _, j in boxes]
        self.word_tops = [b[1] for b, _ in boxes]
        self.max_word_h = max((b[3] for b, _ in boxes), default=0.0)
        order = sorted(range(len(self.lines)), key=lambda i: self.lines[i].yc)
        self.line_order = order
        self.line_centres = [self.lines[i].yc for i in order]
        self.max_line_h = max((l.h for l in self.lines), default=0.0)

    def lines_near(self, yc: float, half: float) -> List[_Line]:
        """Lines whose vertical centre is within ``half`` of ``yc``."""
        lo = bisect.bisect_left(self.line_centres, yc - half)
        hi = bisect.bisect_right(self.line_centres, yc + half)
        return [self.lines[i] for i in self.line_order[lo:hi]]


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


def _is_label_line(line: _Line, pg: _Page) -> bool:
    """A line that is a label row (starts with 'Label:' or is made of printed labels), not a value."""
    return bool(_LABEL_START.match(line.text)) or pg.vocab.is_label(line.text)


def _clean_remainder(text: str, start: int, pg: _Page) -> Tuple[int, int]:
    """Span of the value text after an anchor: separators and format hints skipped, cut before the next label on the line.

    The next label is the earliest of (a) any single label-like word followed by a colon ("Date:") and (b) a printed label of
    the vocabulary, with or without a colon ("Date of Birth:", "ID number")."""
    start = _skip_hints(text, start)
    rest = text[start:]
    start += len(_SEPARATORS.match(rest).group(0)) if _SEPARATORS.match(rest) else 0
    start = _skip_hints(text, start)
    rest = text[start:]
    cuts = [m.start() for m in [_NEXT_LABEL.search(rest)] if m]
    # a vocabulary label cuts the value where it starts the text or spans several words ("Total credits", "ID number");
    # a single generic word inside a value ("NOT A DATE", "Signature Lane") is part of the value, not a label
    cuts += [s_ for s_, e_ in pg.vocab.spans(rest) if s_ == 0 or re.search(r"\s", rest[s_:e_])]
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


def _value_from_line(line: _Line, vs: int, ve: int) -> Tuple[List[int], str]:
    return _words_in_span(line, vs, ve), line.text[vs:ve]


def _wbox(pg: _Page, j: int) -> BBox:
    return tuple(pg.ocr.words[j].bbox)


def _rect_gap(a: BBox, b: BBox) -> float:
    """Distance between two [x, y, w, h] boxes (0 when they touch or overlap)."""
    dx = max(0.0, b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2]))
    dy = max(0.0, b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3]))
    return (dx * dx + dy * dy) ** 0.5


def _is_valid(spec: FieldSpec, cand: _Cand, settings: ExtractorSettings) -> bool:
    """The candidate text is a plausible value of the field's type (format only; the same checks as validationStatus)."""
    if cand.pattern_miss or not cand.raw.strip():
        return False
    normalized, _ = normalize(spec.normalizer, cand.raw, settings.dateOrder)
    if spec.normalizer == "date" and normalized is None and is_ambiguous_numeric_date(cand.raw):
        return True
    if spec.normalizer in ("date", "amount", "digits", "account", "id") and normalized is None:
        return False
    return validate(spec.validators, cand.raw, normalized) != "INVALID_FORMAT"


def _column_bounds(pg: _Page, line: _Line, m: "re.Match[str]", label: BBox) -> Tuple[float, float]:
    """[left, right) x-range of the label's column: from a small tolerance left of the label to the next label on its row."""
    tol = pg.settings.geometry["columnLeftTolerance"]
    left = label[0] - tol
    lx1, lyc, lh = label[0] + label[2], label[1] + label[3] / 2, label[3]
    starts: List[float] = []
    for s_, e_ in pg.vocab.spans(line.text):          # a later label on the same OCR line
        if s_ >= m.end():
            ids = _words_in_span(line, s_, e_)
            if ids:
                starts.append(_wbox(pg, ids[0])[0])
    for other in pg.lines_near(lyc, 0.6 * max(lh, pg.max_line_h)):   # a label on another OCR line of the same row, to the right
        if other.idx == line.idx or abs(other.yc - lyc) > 0.6 * max(lh, other.h):
            continue
        sp = pg.vocab.spans(other.text)
        first = _ALNUM.search(other.text)
        # a label row: its first printed word is a label (stray OCR marks such as a leading "." do not count); the label's
        # own word box gives its position, not the line's left edge
        if not sp or first is None or sp[0][0] > first.start():
            continue
        ids = _words_in_span(other, sp[0][0], sp[0][1])
        if ids and _wbox(pg, ids[0])[0] >= lx1 - 0.005:
            starts.append(_wbox(pg, ids[0])[0])
    right = (min(starts) - tol) if starts else 1.0
    return left, max(right, left)


def _below_rows(pg: _Page, label: BBox, left: float, right: float) -> List[List[int]]:
    """Rows of words below the label inside its column, nearest first (geometry only, never OCR line order).

    A word is in the column when its centre lies in [left, right) or it overlaps the label's own horizontal extent (a
    right-aligned label has its value under it, not under its left edge). Rows closer to the label than 1.8 text heights
    only; the caller decides which row holds the value."""
    yb, hl = label[1] + label[3], label[3]
    lx0, lx1 = label[0], label[0] + label[2]
    cands = []
    reach = yb + 1.8 * max(hl, pg.max_word_h)          # no row further below than this can pass the gap test
    lo = bisect.bisect_left(pg.word_tops, yb - 0.3 * hl)
    hi = bisect.bisect_right(pg.word_tops, reach)
    for j in pg.word_order[lo:hi]:
        b = _wbox(pg, j)
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        if not (left <= cx < right or (b[0] < lx1 and b[0] + b[2] > lx0)):
            continue
        cands.append((cy, b[0], j, b))
    cands.sort()
    rows: List[List[Tuple[float, int, BBox]]] = []     # each row: [(x, word index, box)]
    last: Tuple[float, float] = (0.0, 0.0)             # (centre y, height) of the previous word
    for cy, x, j, b in cands:
        # chain each word to the previous one (sorted by centre y), so a row of a slightly rotated scan, whose words drift
        # vertically along the line, stays one row instead of splitting where the drift exceeds one word height
        if rows and abs(cy - last[0]) <= 0.6 * max(last[1], b[3]):
            rows[-1].append((x, j, b))
        else:
            rows.append([(x, j, b)])
        last = (cy, b[3])
    out: List[List[int]] = []
    for row in rows:
        row_h = max(b[3] for _, _, b in row)
        if min(b[1] for _, _, b in row) - yb > 1.8 * max(hl, row_h):
            break
        out.append([j for _, j, _ in sorted(row)])
    return out


def _row_as_line(pg: _Page, ids: List[int]) -> _Line:
    parts, spans, pos = [], [], 0
    for j in ids:
        t = pg.ocr.words[j].text.strip()
        spans.append((j, pos, pos + len(t)))
        parts.append(t)
        pos += len(t) + 1
    boxes = [_wbox(pg, j) for j in ids]
    return _Line(-1, " ".join(parts), tuple(spans), _union(boxes))


def _right_row(pg: _Page, line: _Line, label: BBox, right_bound: float) -> Optional[_Line]:
    """The OCR line to the right of the label on the same row (nearest first)."""
    lx1, lyc, lh = label[0] + label[2], label[1] + label[3] / 2, label[3]
    row = [l for l in pg.lines_near(lyc, 0.6 * max(lh, pg.max_line_h))
           if l.idx != line.idx and abs(l.yc - lyc) <= 0.6 * max(l.h, lh) and l.x >= lx1 - 0.01]
    return min(row, key=lambda l: l.x) if row else None


def _anchor_candidates(spec: FieldSpec, strat: Strategy, pg: _Page) -> List[_Cand]:
    cands: List[_Cand] = []
    examined = 0
    for line in pg.lines:
        m = _find_anchor(line, strat.anchors)
        if not m:
            continue
        examined += 1
        if examined > _MAX_ANCHOR_LINES:
            break
        if any(s_ <= m.start() and e_ >= m.end() and e_ - s_ > m.end() - m.start() for s_, e_ in pg.vocab.spans(line.text)):
            continue    # e.g. "full name" inside "Guarantor full name": that label belongs to another person's field
        label_ids = _words_in_span(line, m.start(), m.end())
        label = _union([_wbox(pg, j) for j in label_ids]) if label_ids else line.bbox
        options: List[Tuple[float, int, _Cand, _Line]] = []   # (distance, preference, candidate, line the value is on)

        def add(kind: str, tline: _Line, ts: int, te: int, pref: int) -> None:
            if te <= ts or not _has_content(tline.text[ts:te]):
                return
            raw_text = tline.text[ts:te]
            if _is_hint(raw_text) or pg.vocab.is_label(raw_text):
                return                                  # a label or a format hint is never a value
            s_, e_, miss, distinct = _narrow(tline.text, ts, te, strat)
            words, raw = _value_from_line(tline, s_, e_)
            if not words or not raw.strip() or _is_hint(raw) or pg.vocab.is_label(raw):
                return
            cand = _Cand(words, raw, "anchor", kind, 0, miss, distinct, line.idx, anchor_pos=(label[1], label[0]))
            cand.valid = _is_valid(spec, cand, pg.settings)
            box = _union([_wbox(pg, j) for j in words])
            # a value on the label's own OCR line is adjacent by construction (its horizontal gap and a vertical gap to a
            # line below are not comparable); right-of-label and below-label candidates compete on real box distance
            options.append((0.0 if kind == "same" else _rect_gap(label, box), pref, cand, tline))

        # 1. same line, after the label
        vs, ve = _clean_remainder(line.text, m.end(), pg)
        add("same", line, vs, ve, 0)
        # 2. to the right on the same row (another OCR line)
        right = _right_row(pg, line, label, 1.0)
        if right is not None:
            rs, re_ = _clean_remainder(right.text, 0, pg)
            if not _is_label_line(right, pg):
                add("right", right, rs, re_, 1)
        # 3. below the label, inside its column: the first row that holds a value. A row of pure labels ends the search
        #    (the value is missing); a row that starts with another label belongs to another column and is skipped.
        left_b, right_b = _column_bounds(pg, line, m, label)
        for ids in _below_rows(pg, label, left_b, right_b)[:4]:
            row = _row_as_line(pg, ids)
            if _is_label_line(row, pg):
                break
            bs, be = _clean_remainder(row.text, 0, pg)
            if be > bs and _has_content(row.text[bs:be]):
                add("below", row, bs, be, 2)
                break
        if not options:
            continue
        # nearest candidate that validates for the field type; otherwise the nearest one, flagged (never dropped)
        options.sort(key=lambda o: (o[0], o[1]))
        chosen = next((o for o in options if o[2].valid), options[0])
        _, _, cand, tline = chosen
        last_pos = pg.word_line.get(cand.words[0])
        cand.last_line = last_pos
        # multi-line values (addresses): append following unlabeled lines
        prev = pg.lines[last_pos] if last_pos is not None else tline
        for _ in range(spec.continuationLines):
            nxt = _line_below(prev, pg, line.x)
            if nxt is None:
                break
            ns, ne = _clean_remainder(nxt.text, 0, pg)
            if ne <= ns:
                break
            w2, r2 = _value_from_line(nxt, ns, ne)
            cand.words += w2
            cand.raw += " " + r2
            cand.extra_lines += 1
            prev = nxt
        cands.append(cand)
    return cands


def _line_below(prev: _Line, pg: _Page, min_left: float) -> Optional[_Line]:
    below = [l for l in pg.lines if l.idx != prev.idx and l.y >= prev.y + prev.h - 0.3 * prev.h
             and l.y - (prev.y + prev.h) <= 1.8 * prev.h and l.x >= min_left - 0.03]
    for line in sorted(below, key=lambda l: (l.y, l.x)):
        if abs(line.yc - prev.yc) <= 0.3 * prev.h:
            continue  # same row, not below
        if _is_label_line(line, pg) or not _has_content(line.text):
            return None
        return line
    return None


def _composite_candidates(spec: FieldSpec, strat: Strategy, pg: _Page) -> List[_Cand]:
    words: List[int] = []
    raws: List[str] = []
    placement_rank = {"same": 0, "right": 1, "below": 2}
    placement, miss = "same", False
    for part in strat.parts:
        sub = Strategy("anchor", strat.confidence, anchors=part, pattern=strat.pattern, pick=strat.pick)
        found = _anchor_candidates(FieldSpec(spec.name, spec.semanticType, spec.normalizer, spec.validators, (sub,)),
                                   sub, pg)
        if not found:
            return []
        first = min(found, key=lambda c: (not c.valid, c.anchor_pos))
        words += first.words
        raws.append(first.raw)
        miss = miss or first.pattern_miss or not first.valid
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


def _extract_field(pg: _Page, page_no: int, spec: FieldSpec, settings: ExtractorSettings) -> Optional[FieldResult]:
    page = pg.ocr
    for strat in spec.strategies:
        if strat.kind == "anchor":
            cands = _anchor_candidates(spec, strat, pg)
        elif strat.kind == "composite":
            cands = _composite_candidates(spec, strat, pg)
        else:
            cands = _region_candidates(strat, pg.lines, page)
        if not cands:
            continue

        def box_of(c: _Cand) -> BBox:
            return c.region_box if c.region_box else _union([tuple(page.words[j].bbox) for j in c.words])

        def rank(c: _Cand):
            # a value that validates for the field type first, then the expected region, then reading order by GEOMETRY
            # (top to bottom, left to right) of the label, not the order the OCR engine emitted its lines
            inside = spec.region is None or _inside(box_of(c), spec.region)
            return (not c.valid, not inside, c.pattern_miss, round(c.anchor_pos[0], 3), c.anchor_pos[1], c.anchor_line)

        best = sorted(cands, key=rank)[0]
        best.distinct = max(best.distinct, len({c.raw.strip().lower() for c in cands}))
        box = box_of(best)
        notes: List[str] = [f"strategy={best.strategy}", f"placement={best.placement}"]
        if best.pattern_miss:
            notes.append("text under the label did not match the expected pattern")
        elif not best.valid and best.strategy != "anchor_region":
            notes.append("no candidate near the label validated for this field type; nearest non-label text returned")
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
    anchors = [a for f in schema.fields for s in f.strategies for a in (s.anchors or tuple(x for p in s.parts for x in p))]
    vocab = _vocab(list(anchors) + list(schema.labels) + list(settings.labelVocabulary))
    word_line = {j: pos for pos, line in enumerate(lines) for j, _, _ in line.spans}
    pg = _Page(page, lines, vocab, settings, word_line)
    out = ExtractionOutcome()
    for spec in schema.fields:
        result = _extract_field(pg, page.page, spec, settings)
        if result is None:
            out.missing.append(spec.name)
        else:
            out.results.append(result)
    return out
