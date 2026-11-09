"""Thresholds for the text-layer-vs-render check (O6): config/text_layer_check.json (override: TEXT_LAYER_CHECK_CONFIG).

The JSON file is the single source of truth (no defaults in code). Every value is a plain number, bool or string, so the
whole configuration is echoed verbatim into provenance (``cfg.<name>`` keys): a reviewer can see which thresholds produced
a signal. NaN/Infinity and nonsensical values are refused at load time.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Union

from .config import SERVICE_ROOT

DEFAULT_PATH = SERVICE_ROOT / "config" / "text_layer_check.json"


class TextLayerCheckConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class TextLayerCheckConfig:
    configVersion: str
    textSimilarityMin: float          # a layer word and the OCR text at the same place match at or above this similarity
    numericSimilarityMin: float       # stricter: a word with a digit differs from its OCR reading by one digit, that is a different value
    minWordChars: int                 # words with fewer letters/digits than this are ignored (punctuation, bullets)
    minOcrConfidence: float           # OCR words below this are not trusted as "visible text"
    candidatePadFrac: float           # boxes are grown by this share of their height before testing overlap
    differsMinOverlap: float          # "the same place": this share of the layer word must lie under one OCR word ...
    differsMinOcrShare: float         # ... and this share of that OCR word must lie over the layer word (an inflated OCR box is not enough)
    inkDelta: int                     # a pixel is "ink" when it differs from the local background by more than this
    inkMinFraction: float             # share of ink pixels under which a word's area counts as blank (nothing visible)
    inkPadPx: int
    minBoxHeightPx: int               # layer words shorter than this (pixels) are degenerate boxes and are ignored
    edgeMarginFrac: float             # words touching the page edge (clipped glyphs) are not judged: this share of the page on each side
    tinyHeightPx: int                 # an unread layer word shorter than this cannot be read by anyone: reported as hidden
    groupGapFactor: float             # neighbouring mismatching words closer than this (times their height) form one signal
    minGroupChars: int                # a mismatch shorter than this many letters/digits is a "tiny difference" and ignored
    rereadScale: float                # a "differs" candidate is read again from a re-render this many times sharper (0 = off)
    rereadPadFrac: float              # margin around the word for the second reading, as a share of the word height
    rereadTimeoutS: float
    maxRereadsPerPage: int            # candidates beyond this are reported unverified
    flagRenderOnly: bool              # also report visible words that are absent from the text layer (noisier; see README)
    minPageAgreement: float           # below this share of matching layer words the page is reported as a whole, not per word
    minWordsForAgreement: int
    minOcrCoverage: float             # OCR must have read at least this share as many words as the layer holds, else the page is "not comparable"
    maxWordsPerPage: int              # more words than this on one page (either side): the page is not compared
    maxSignalsPerPage: int
    hiddenConfidence: float
    hiddenConfidencePerWord: float
    hiddenConfidenceMax: float
    differsConfidence: float
    renderOnlyConfidence: float
    pageLevelConfidence: float

    def as_provenance(self) -> Dict[str, Union[float, int, str]]:
        # provenance values are numbers or strings (never JSON booleans), so a flag is recorded as 0 / 1
        return {f"cfg.{f.name}": (int(v) if isinstance(v, bool) else v) for f in fields(self) for v in [getattr(self, f.name)]}


def _validate(cfg: TextLayerCheckConfig) -> None:
    problems = []
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{f.name} must be a finite number")
    for name in ("textSimilarityMin", "numericSimilarityMin", "minOcrCoverage", "differsMinOverlap", "differsMinOcrShare", "minOcrConfidence", "inkMinFraction", "minPageAgreement", "hiddenConfidence",
                 "hiddenConfidenceMax", "differsConfidence", "renderOnlyConfidence", "pageLevelConfidence"):
        if not 0 <= getattr(cfg, name) <= 1:
            problems.append(f"{name} must be in [0, 1]")
    if not 0 < cfg.textSimilarityMin <= cfg.numericSimilarityMin:
        problems.append("textSimilarityMin must be > 0 and <= numericSimilarityMin")
    for name in ("minWordChars", "minBoxHeightPx", "tinyHeightPx", "minGroupChars", "minWordsForAgreement", "maxWordsPerPage",
                 "maxSignalsPerPage"):
        if getattr(cfg, name) < 1:
            problems.append(f"{name} must be >= 1")
    for name in ("candidatePadFrac", "groupGapFactor", "hiddenConfidencePerWord", "inkPadPx", "rereadPadFrac", "edgeMarginFrac"):
        if getattr(cfg, name) < 0:
            problems.append(f"{name} must be >= 0")
    if cfg.rereadScale != 0 and not 1 <= cfg.rereadScale <= 4:
        problems.append("rereadScale must be 0 (off) or in [1, 4]")
    if not 0 < cfg.rereadTimeoutS <= 300:
        problems.append("rereadTimeoutS must be in (0, 300]")
    if cfg.maxRereadsPerPage < 0:
        problems.append("maxRereadsPerPage must be >= 0")
    if cfg.tinyHeightPx < cfg.minBoxHeightPx:
        problems.append("tinyHeightPx must be >= minBoxHeightPx")
    if cfg.edgeMarginFrac >= 0.1:
        problems.append("edgeMarginFrac must be < 0.1")
    if not 0 <= cfg.inkDelta <= 255:
        problems.append("inkDelta must be in [0, 255]")
    if cfg.hiddenConfidence > cfg.hiddenConfidenceMax:
        problems.append("hiddenConfidence must be <= hiddenConfidenceMax")
    if problems:
        raise TextLayerCheckConfigError("invalid text-layer check configuration: " + "; ".join(problems))


def load_text_layer_check_config(path: Union[str, Path, None] = None) -> TextLayerCheckConfig:
    source = Path(path) if path else Path(os.environ.get("TEXT_LAYER_CHECK_CONFIG") or DEFAULT_PATH)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TextLayerCheckConfigError(f"cannot read {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise TextLayerCheckConfigError(f"{source.name} must contain a JSON object")
    names = {f.name: f for f in fields(TextLayerCheckConfig)}
    missing = sorted(set(names) - set(data))
    extra = sorted(set(data) - set(names))
    if missing or extra:
        raise TextLayerCheckConfigError(f"{source.name}: missing {missing or 'nothing'}, unknown {extra or 'nothing'}")
    values = {}
    for name, f in names.items():
        raw = data[name]
        kind = {"str": str, "int": int, "float": float, "bool": bool}[f.type if isinstance(f.type, str) else f.type.__name__]
        if kind is bool:
            ok = isinstance(raw, bool)
        elif kind is str:
            ok = isinstance(raw, str)
        elif kind is int:
            ok = isinstance(raw, int) and not isinstance(raw, bool)
        else:
            ok = isinstance(raw, (int, float)) and not isinstance(raw, bool)
        if not ok:
            raise TextLayerCheckConfigError(f"{source.name}: {name} must be a {kind.__name__}")
        values[name] = kind(raw)
    cfg = TextLayerCheckConfig(**values)
    _validate(cfg)
    return cfg
