"""Table layout from OCR geometry (pure: no I/O, no OCR engine, no contract objects).

Works on the words of one OcrPage (bboxes are [x, y, w, h] fractions of the page). A bank-statement table is read as:
  1. words are grouped into rows by vertical centre;
  2. a header row names the columns (Date / Description / Debit / Credit / Balance, vocabulary from config);
  3. each later row that starts with a date is a transaction; its amount words are assigned to the nearest amount
     column by right edge, centre or left edge (right-aligned numbers share a right edge, not a left edge);
  4. the first later row that starts with "Total(s)" supplies the stated totals; the table ends there.
Nothing is guessed: an amount that cannot be assigned or parsed is kept as an unreadable cell, never dropped.
A page without its own header can inherit the columns of the previous page (statements that continue over pages).
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .extract_normalize import normalize_amount
from .layout_config import LayoutConfig
from .models import OcrPage, OcrWord

Box = Tuple[float, float, float, float]  # x, y, w, h (fractions)
AMOUNT_ROLES = ("debit", "credit", "balance")
_NUMERIC = re.compile(r"^[(\-−–—]?[^\w\s]?\d[\d,.]*\)?$")


def union_box(boxes: Sequence[Box]) -> Box:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class Column:
    role: str
    left: float
    right: float

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2


@dataclass
class Cell:
    text: str
    bbox: Box
    ocr_confidence: float
    normalized: Optional[str]          # plain decimal, None when the text is not a readable amount
    ambiguous: bool = False            # the word sits almost as close to a second column


@dataclass
class TxnRow:
    date: str
    description: str
    cells: Dict[str, Cell] = field(default_factory=dict)   # role -> Cell
    problems: List[str] = field(default_factory=list)


@dataclass
class PageTable:
    columns: Dict[str, Column]
    columns_inherited: bool
    rows: List[TxnRow] = field(default_factory=list)
    totals: Dict[str, Cell] = field(default_factory=dict)
    totals_problems: List[str] = field(default_factory=list)
    skew_deg: float = 0.0              # page tilt that was compensated when grouping rows
    orphan_amounts: int = 0            # amount-like words in rows that are neither transactions nor totals


def _cluster(ys: Sequence[float], tol: float) -> List[int]:
    """Row index per value (values given in any order), joining a value to the row whose running mean is within tol."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    labels = [0] * len(ys)
    row = -1
    mean = 0.0
    count = 0
    for i in order:
        if row >= 0 and abs(ys[i] - mean) < tol:
            mean = (mean * count + ys[i]) / (count + 1)
            count += 1
        else:
            row += 1
            mean, count = ys[i], 1
        labels[i] = row
    return labels


def estimate_skew_deg(words: Sequence[OcrWord], width_px: int, height_px: int, cfg: LayoutConfig) -> float:
    """Small page tilt (degrees, positive = text runs downhill to the right) that makes the words fall into the
    fewest rows. 0.0 unless a tilt is clearly better than none (scans are often a degree or so off)."""
    if len(words) < 8 or cfg.skewMaxDeg <= 0:
        return 0.0
    cx = [(w.bbox[0] + w.bbox[2] / 2) * width_px for w in words]
    cy = [(w.bbox[1] + w.bbox[3] / 2) * height_px for w in words]
    tol = statistics.median(w.bbox[3] for w in words) * height_px * cfg.rowYToleranceFactor
    if tol <= 0:
        return 0.0

    def rows_at(deg: float) -> int:
        t = math.tan(math.radians(deg))
        return max(_cluster([y - x * t for x, y in zip(cx, cy)], tol)) + 1

    base = rows_at(0.0)
    steps = int(round(cfg.skewMaxDeg / cfg.skewStepDeg))
    counts = {k: rows_at(k * cfg.skewStepDeg) for k in range(-steps, steps + 1)}
    best_rows = min(counts.values())
    # only a clear improvement counts (at least 3% fewer rows); many angles tie at the minimum, so take the middle
    # of the plateau around the tilt closest to zero rather than its first edge
    if best_rows > base * 0.97:
        return 0.0
    plateau = sorted(k for k, n in counts.items() if n == best_rows)
    runs, run = [], [plateau[0]]
    for k in plateau[1:]:
        if k == run[-1] + 1:
            run.append(k)
        else:
            runs.append(run)
            run = [k]
    runs.append(run)
    chosen = min(runs, key=lambda r: min(abs(k) for k in r))
    return round(statistics.fmean(chosen) * cfg.skewStepDeg, 4)


def group_rows(words: Sequence[OcrWord], cfg: LayoutConfig, width_px: int = 1000, height_px: int = 1000,
               skew_deg: float = 0.0) -> List[List[OcrWord]]:
    """Rows top to bottom, each sorted left to right. ``skew_deg`` tilts the row axis (see estimate_skew_deg)."""
    if not words:
        return []
    median_h = statistics.median(w.bbox[3] for w in words) * height_px
    tol = max(median_h * cfg.rowYToleranceFactor, 1e-6)
    t = math.tan(math.radians(skew_deg))
    ys = [(w.bbox[1] + w.bbox[3] / 2) * height_px - (w.bbox[0] + w.bbox[2] / 2) * width_px * t for w in words]
    labels = _cluster(ys, tol)
    rows: List[List[OcrWord]] = [[] for _ in range(max(labels) + 1)]
    for w, lab in zip(words, labels):
        rows[lab].append(w)
    return [sorted(r, key=lambda w: w.bbox[0]) for r in rows]


def _clean(text: str) -> str:
    return re.sub(r"[^\w ]", "", text.lower()).strip()


def find_header(rows: List[List[OcrWord]], cfg: LayoutConfig) -> Optional[Tuple[int, Dict[str, Column]]]:
    """(row index, {role: Column}) of the first row naming at least ``minHeaderRoles`` distinct roles."""
    vocab = [("date", cfg.headerDate), ("description", cfg.headerDescription), ("debit", cfg.headerDebit),
             ("credit", cfg.headerCredit), ("balance", cfg.headerBalance)]
    phrases = sorted(((role, kw.split()) for role, kws in vocab for kw in kws), key=lambda p: -len(p[1]))
    for index, row in enumerate(rows):
        tokens = [_clean(w.text) for w in row]
        used = [False] * len(row)
        found: Dict[str, List[int]] = {}
        for role, parts in phrases:
            if role in found:
                continue
            n = len(parts)
            for start in range(len(row) - n + 1):
                span = range(start, start + n)
                if all(not used[i] for i in span) and [tokens[i] for i in span] == parts:
                    found[role] = list(span)
                    for i in span:
                        used[i] = True
                    break
        if len(found) >= cfg.minHeaderRoles:
            columns = {}
            for role, span in found.items():
                box = union_box([tuple(row[i].bbox) for i in span])  # type: ignore[arg-type]
                columns[role] = Column(role, box[0], box[0] + box[2])
            return index, columns
    return None


def _is_date(text: str, cfg: LayoutConfig) -> bool:
    return any(re.match(p, text.strip()) for p in cfg.datePatterns)


def _amount_like(text: str) -> bool:
    return bool(_NUMERIC.match(text.strip()))


def _nearest_column(word: OcrWord, columns: Dict[str, Column], cfg: LayoutConfig) -> Optional[Tuple[str, bool]]:
    x, _, w, _ = word.bbox
    left, right, cx = x, x + w, x + w / 2
    scored = []
    for role in AMOUNT_ROLES:
        col = columns.get(role)
        if col is None:
            continue
        scored.append((min(abs(right - col.right), abs(cx - col.cx), abs(left - col.left)), role))
    if not scored:
        return None
    scored.sort()
    best_dist, best = scored[0]
    if best_dist > cfg.amountColumnMaxDistance:
        return None
    ambiguous = len(scored) > 1 and scored[1][0] - best_dist < 0.01
    return best, ambiguous


def _starts_with_total(row: List[OcrWord], cfg: LayoutConfig) -> bool:
    return bool(row) and _clean(row[0].text) in cfg.totalsKeywords


def _read_amounts(row: List[OcrWord], columns: Dict[str, Column], cfg: LayoutConfig,
                  problems: List[str], orig: Dict[int, Box]) -> Tuple[Dict[str, Cell], int]:
    """Assign amount-like words of one row to columns. Returns (cells, amount-like words left unassigned)."""
    cells: Dict[str, Cell] = {}
    unassigned = 0
    for w in row:
        if not _amount_like(w.text):
            continue
        hit = _nearest_column(w, columns, cfg)
        if hit is None:
            unassigned += 1
            continue
        role, ambiguous = hit
        if role in cells:
            problems.append(f"two values in the {role} column")
            continue
        cells[role] = Cell(text=w.text, bbox=orig.get(id(w), tuple(w.bbox)), ocr_confidence=w.ocrConfidence,  # type: ignore[arg-type]
                           normalized=normalize_amount(w.text), ambiguous=ambiguous)
    return cells, unassigned


def _deskewed(page: OcrPage, skew_deg: float) -> Tuple[List[OcrWord], Dict[int, Box]]:
    """Words with boxes sheared back by the page tilt (so rows are horizontal and columns vertical), plus a map from
    each returned word to its ORIGINAL box: reported regions always use the original page coordinates."""
    if skew_deg == 0.0:
        return list(page.words), {id(w): tuple(w.bbox) for w in page.words}  # type: ignore[misc]
    t = math.tan(math.radians(skew_deg))
    W, H = page.widthPx, page.heightPx
    out: List[OcrWord] = []
    orig: Dict[int, Box] = {}
    for w in page.words:
        x, y, bw, bh = w.bbox
        cx, cy = (x + bw / 2) * W, (y + bh / 2) * H
        ncx, ncy = cx + cy * t, cy - cx * t
        moved = w.model_copy(update={"bbox": ((ncx / W) - bw / 2, (ncy / H) - bh / 2, bw, bh)})
        out.append(moved)
        orig[id(moved)] = tuple(w.bbox)  # type: ignore[assignment]
    return out, orig


def parse_page(page: OcrPage, cfg: LayoutConfig,
               inherited: Optional[Dict[str, Column]] = None) -> Optional[PageTable]:
    """The statement table on one page, or None when the page has no recognisable table."""
    skew = estimate_skew_deg(page.words, page.widthPx, page.heightPx, cfg)
    words, orig = _deskewed(page, skew)
    rows = group_rows(words, cfg)
    header = find_header(rows, cfg)
    if header is not None:
        start, columns, inh = header[0] + 1, header[1], False
    elif inherited:
        start, columns, inh = 0, inherited, True
    else:
        return None
    if not any(r in columns for r in ("debit", "credit")) or "date" not in columns and not inh:
        return None
    table = PageTable(columns=columns, columns_inherited=inh, skew_deg=skew)
    desc_left = columns["description"].left if "description" in columns else None
    first_amount_left = min((columns[r].left for r in AMOUNT_ROLES if r in columns), default=1.0)
    for row in rows[start:]:
        if _starts_with_total(row, cfg):
            problems: List[str] = []
            cells, _ = _read_amounts(row, columns, cfg, problems, orig)
            table.totals = {r: c for r, c in cells.items() if r in ("debit", "credit")}
            table.totals_problems = problems
            break
        # the date cell: leading words left of the description / amount columns
        limit = (desc_left - 0.005) if desc_left is not None else first_amount_left
        lead = [w for w in row if w.bbox[0] + w.bbox[2] <= limit + 0.02 and w.bbox[0] < limit]
        date_words = next((" ".join(w.text for w in lead[:n]) for n in (1, 2, 3)
                           if _is_date(" ".join(w.text for w in lead[:n]), cfg)), None)
        if date_words is None:
            table.orphan_amounts += sum(1 for w in row if _amount_like(w.text) and _nearest_column(w, columns, cfg))
            continue
        n_date = len(date_words.split())
        rest = row[n_date:]
        problems = []
        cells, unassigned = _read_amounts(rest, columns, cfg, problems, orig)
        amount_ids = {id(w) for w in rest if orig.get(id(w)) in {c.bbox for c in cells.values()}}
        description = " ".join(w.text for w in rest if id(w) not in amount_ids
                               and w.bbox[0] + w.bbox[2] / 2 < first_amount_left + 0.02)
        txn = TxnRow(date=date_words, description=description, cells=cells, problems=problems)
        if not any(r in cells for r in ("debit", "credit")):
            txn.problems.append("no amount found in the debit or credit column")
        if unassigned:
            txn.problems.append(f"{unassigned} amount-like value(s) outside every amount column")
        table.rows.append(txn)
    return table


def balance_chain_breaks(rows: Sequence[TxnRow], tolerance: Decimal = Decimal("0.005")) -> List[int]:
    """Indexes of rows whose printed balance is not the previous balance plus credit minus debit.

    Only consecutive rows that all have a readable balance (and a readable amount) are compared.
    """
    breaks: List[int] = []
    prev: Optional[Decimal] = None
    for i, row in enumerate(rows):
        bal = row.cells.get("balance")
        value = Decimal(bal.normalized) if bal and bal.normalized is not None else None
        deb = row.cells.get("debit")
        cred = row.cells.get("credit")
        readable = all(c.normalized is not None for c in (deb, cred) if c is not None) and (deb or cred)
        if prev is not None and value is not None and readable:
            change = (Decimal(cred.normalized) if cred else Decimal(0)) - (Decimal(deb.normalized) if deb else Decimal(0))
            if abs(prev + change - value) > tolerance:
                breaks.append(i)
        prev = value
    return breaks
