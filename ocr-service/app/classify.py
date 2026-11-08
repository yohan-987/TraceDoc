"""Deterministic, explainable page classification (O3).

``classify_page`` scores one page, from its OCR words/lines only, against four classes (id_card,
application_form, bank_statement, other) and returns exactly one docType:

1. Keyword points: each lexicon rule (app/classifier_lexicon.py) that matches adds its weight, with a small bonus
   for repeats; a combination bonus rewards many DIFFERENT keywords of one class.
2. Layout cues (aspect ratio, line density, "Label:" lines, transaction-table rows, right-aligned amounts) add points
   ONLY to a class that already has ``cueGateRaw`` keyword points, so layout alone never creates a document type.
3. raw points -> score in [0, 1):  score = 1 - exp(-raw / scoreScale).
4. Decision (thresholds in config/classifier.json):
   too few words, fewer than ``minDistinctKeywords`` distinct keywords in the best class, or best score < ``minScore``
   -> UNKNOWN;  best - second < ``ambiguityMargin`` -> AMBIGUOUS;  otherwise the best class.
5. docTypeConfidence = best - confidenceSeparationPenalty * second  (clamped to 0..1) for a class or AMBIGUOUS;
   for UNKNOWN it is the strength of the best, insufficient evidence (never above minScore).

Confidence says how strongly the text and layout resemble the label. It is not a severity or a verdict about the
document, and it says nothing about authenticity.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .classifier_config import ClassifierConfig
from .classifier_lexicon import CLASSES, LEXICON, LEXICON_VERSION, TABLE_HEADER_TERMS

_RULES = {
    cls: [(label, re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])"), weight, kind)
          for label, pattern, weight, kind in rules]
    for cls, rules in LEXICON.items()
}
_HEADER_TERMS = [re.compile(rf"(?<![a-z0-9])(?:{t})(?![a-z0-9])") for t in TABLE_HEADER_TERMS]
_DATE = re.compile(
    r"(?<![\d/.-])(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s?\d{2,4})(?![\d/-])")
_AMOUNT = re.compile(r"(?<![\d.,/-])-?\(?\d{1,3}(?:,\d{3})*\.\d{2}\)?(?![\d/.-]|,\d)")
_LABEL_LINE = re.compile(r"^[^:]{2,40}:")

DOC_UNKNOWN = "UNKNOWN"
DOC_AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Evidence:
    kind: str      # title | kw | combo | aspect | density | labels | table_rows | table_header | amounts_right
    label: str
    count: int
    points: float

    def short(self) -> str:
        return f"{self.kind}:{self.label}x{self.count}={self.points:.2f}"


@dataclass(frozen=True)
class PageClassification:
    page: int
    docType: str
    confidence: float
    scores: Dict[str, float]
    raw: Dict[str, float]
    evidence: Dict[str, List[Evidence]] = field(default_factory=dict)
    note: Optional[str] = None
    lexiconVersion: str = LEXICON_VERSION


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("\u2019", "'")).strip()


def _keyword_points(text: str, cls: str, cfg: ClassifierConfig) -> Tuple[float, int, List[Evidence]]:
    points, distinct, evidence = 0.0, 0, []
    for label, rx, weight, kind in _RULES[cls]:
        count = len(rx.findall(text))
        if not count:
            continue
        gain = weight * (1 + cfg.frequencyBonus * (min(count, cfg.frequencyCap) - 1))
        points += gain
        distinct += 1
        evidence.append(Evidence(kind, label, count, gain))
    extra = max(0, distinct - cfg.comboFreeKeywords)
    if extra:
        bonus = cfg.comboBonusPerKeyword * extra
        points += bonus
        evidence.append(Evidence("combo", f"{distinct} distinct keywords", distinct, bonus))
    return points, distinct, evidence


@dataclass(frozen=True)
class _Layout:
    aspect: float
    n_lines: int
    label_lines: int
    table_rows: int
    has_header: bool
    amounts_right_ratio: float
    amount_count: int


def _layout(page: Any, line_texts: List[str]) -> _Layout:
    width, height = max(1, page.widthPx), max(1, page.heightPx)
    label_lines = sum(1 for t in line_texts if _LABEL_LINE.match(t))
    table_rows = sum(1 for t in line_texts if _DATE.search(t) and _AMOUNT.search(t))
    has_header = any(sum(1 for rx in _HEADER_TERMS if rx.search(t)) >= 2 for t in line_texts)
    amount_words = [w for w in page.words if _AMOUNT.search(w.text.lower())]
    right = [w for w in amount_words if (w.bbox[0] + w.bbox[2] / 2) >= 0.55]
    ratio = len(right) / len(amount_words) if amount_words else 0.0
    return _Layout(width / height, len(line_texts), label_lines, table_rows, has_header, ratio, len(amount_words))


def _cue_points(cls: str, lay: _Layout, cfg: ClassifierConfig) -> List[Evidence]:
    cues: List[Evidence] = []
    portrait = cfg.portraitAspectMin <= lay.aspect <= cfg.portraitAspectMax
    if cls == "id_card":
        if cfg.idCardAspectMin <= lay.aspect <= cfg.idCardAspectMax:
            cues.append(Evidence("aspect", f"card-like {lay.aspect:.2f}", 1, cfg.idCardAspectBonus))
        if 3 <= lay.n_lines <= cfg.idCardMaxLines:
            cues.append(Evidence("density", f"{lay.n_lines} lines", lay.n_lines, cfg.idCardSparseBonus))
    elif cls == "application_form":
        if portrait:
            cues.append(Evidence("aspect", f"portrait {lay.aspect:.2f}", 1, cfg.portraitBonus))
        if lay.n_lines and lay.label_lines >= cfg.formLabelLinesMin \
                and lay.label_lines / lay.n_lines >= cfg.formLabelLineRatioMin:
            cues.append(Evidence("labels", "'Label:' lines", lay.label_lines, cfg.formLabelBonus))
    elif cls == "bank_statement":
        if portrait:
            cues.append(Evidence("aspect", f"portrait {lay.aspect:.2f}", 1, cfg.portraitBonus))
        if lay.table_rows >= cfg.transactionRowsMin:
            rows = min(lay.table_rows, cfg.transactionRowCap)
            cues.append(Evidence("table_rows", "date+amount rows", lay.table_rows, rows * cfg.transactionRowBonus))
            if lay.has_header:
                cues.append(Evidence("table_header", "header row", 1, cfg.transactionHeaderBonus))
            if lay.amount_count >= cfg.transactionRowsMin and lay.amounts_right_ratio >= cfg.rightAlignedRatioMin:
                cues.append(Evidence("amounts_right", f"{lay.amounts_right_ratio:.0%} right", lay.amount_count,
                                     cfg.rightAlignedAmountBonus))
    return cues


def classify_page(page: Any, cfg: ClassifierConfig) -> PageClassification:
    """Classify one OcrPage (anything with widthPx, heightPx, words[text,bbox], lines[text])."""
    text = _normalize_text(" ".join(w.text for w in page.words))
    line_texts = [_normalize_text(line.text) for line in page.lines]
    lay = _layout(page, line_texts)

    raw: Dict[str, float] = {}
    distinct: Dict[str, int] = {}
    evidence: Dict[str, List[Evidence]] = {}
    for cls in CLASSES:
        points, count, items = _keyword_points(text, cls, cfg)
        distinct[cls] = count
        if points >= cfg.cueGateRaw:
            for cue in _cue_points(cls, lay, cfg):
                points += cue.points
                items.append(cue)
        raw[cls], evidence[cls] = points, items
    scores = {cls: 1.0 - math.exp(-raw[cls] / cfg.scoreScale) for cls in CLASSES}

    ranked = sorted(CLASSES, key=lambda c: (-scores[c], CLASSES.index(c)))
    best, second = ranked[0], ranked[1]
    s1, s2 = scores[best], scores[second]
    n_words = len(page.words)

    def result(doc_type: str, confidence: float, note: Optional[str]) -> PageClassification:
        return PageClassification(page.page, doc_type, round(min(1.0, max(0.0, confidence)), 4),
                                  {c: round(v, 4) for c, v in scores.items()},
                                  {c: round(v, 4) for c, v in raw.items()}, evidence, note)

    if n_words < cfg.minWords:
        return result(DOC_UNKNOWN, min(s1, cfg.minScore) if n_words else 0.0,
                      f"too little text ({n_words} words, minimum {cfg.minWords})")
    if distinct[best] < cfg.minDistinctKeywords:
        return result(DOC_UNKNOWN, min(s1, cfg.minScore),
                      f"fewer than {cfg.minDistinctKeywords} distinct keywords for any class")
    if s1 < cfg.minScore:
        return result(DOC_UNKNOWN, s1, f"best score {s1:.2f} below minimum {cfg.minScore:.2f}")
    confidence = s1 - cfg.confidenceSeparationPenalty * s2
    if s1 - s2 < cfg.ambiguityMargin:
        return result(DOC_AMBIGUOUS, confidence,
                      f"{best} {s1:.2f} and {second} {s2:.2f} are within margin {cfg.ambiguityMargin:.2f}")
    return result(best, confidence, None)


# ------------------------------------------------------------------ shared context

_STORE_KEY = ("page-classification",)


def classification_store(ctx: Any) -> Dict[int, PageClassification]:
    """Per-request {page: PageClassification}, filled by the page.classifier analyzer."""
    return ctx.get_or_compute(_STORE_KEY, dict)


def apply_page_classifications(ctx: Any, records: List[Any]) -> List[Any]:
    """PageRecords with docType/docTypeConfidence taken from the classifier; untouched if it did not run.

    New records are built from explicit fields so unset optional keys (docTypeOverriddenBy) stay unset.
    """
    store = classification_store(ctx)
    out = []
    for record in records:
        found = store.get(record.page)
        if found is None:
            out.append(record)
            continue
        kwargs = dict(docId=record.docId, page=record.page, renderPath=record.renderPath,
                      renderDpi=record.renderDpi, rotationDeg=record.rotationDeg, deskewDeg=record.deskewDeg,
                      docType=found.docType, docTypeConfidence=found.confidence)
        if record.docTypeOverriddenBy is not None:
            kwargs["docTypeOverriddenBy"] = record.docTypeOverriddenBy
        out.append(type(record)(**kwargs))
    return out
