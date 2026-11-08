"""Page-classifier thresholds: loaded from config/classifier.json (override the path with CLASSIFIER_CONFIG).

The JSON file is the single source of truth (no defaults in code). Every value is a plain number or string, so
the whole configuration is echoed verbatim into the analyzer's provenance (``cfg.<name>`` keys).
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Union

from .config import SERVICE_ROOT

DEFAULT_PATH = SERVICE_ROOT / "config" / "classifier.json"


class ClassifierConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class ClassifierConfig:
    configVersion: str
    scoreScale: float                    # raw points -> score: 1 - exp(-raw / scoreScale)
    minScore: float                      # best score below this -> UNKNOWN
    ambiguityMargin: float               # best - second below this -> AMBIGUOUS
    minWords: int                        # fewer OCR words than this -> UNKNOWN
    minDistinctKeywords: int             # the winning class needs this many different keyword rules
    confidenceSeparationPenalty: float   # confidence = best - penalty * second
    frequencyBonus: float                # extra weight share per repeated keyword
    frequencyCap: int
    comboFreeKeywords: int               # distinct keywords before the combination bonus starts
    comboBonusPerKeyword: float
    cueGateRaw: float                    # layout cues count only if keyword points reach this
    idCardAspectMin: float
    idCardAspectMax: float
    idCardAspectBonus: float
    idCardMaxLines: int
    idCardSparseBonus: float
    portraitAspectMin: float
    portraitAspectMax: float
    portraitBonus: float
    formLabelLinesMin: int
    formLabelLineRatioMin: float
    formLabelBonus: float
    transactionRowsMin: int
    transactionRowBonus: float
    transactionRowCap: int
    transactionHeaderBonus: float
    rightAlignedRatioMin: float
    rightAlignedAmountBonus: float

    def as_provenance(self) -> Dict[str, Union[float, int, str]]:
        """Every threshold as a flat scalar mapping, keyed ``cfg.<name>``."""
        return {f"cfg.{f.name}": getattr(self, f.name) for f in fields(self)}


def _validate(cfg: ClassifierConfig) -> None:
    problems = []
    for f in fields(cfg):  # json.loads accepts NaN / Infinity: they would poison every score (and break the JSON response)
        value = getattr(cfg, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{f.name} must be a finite number")
    if cfg.scoreScale <= 0:
        problems.append("scoreScale must be > 0")
    if not 0 < cfg.minScore < 1:
        problems.append("minScore must be between 0 and 1 (exclusive)")
    if not 0 <= cfg.ambiguityMargin < 1:
        problems.append("ambiguityMargin must be in [0, 1)")
    if not 0 <= cfg.confidenceSeparationPenalty <= 1:
        problems.append("confidenceSeparationPenalty must be in [0, 1]")
    if cfg.minWords < 0 or cfg.minDistinctKeywords < 1:
        problems.append("minWords must be >= 0 and minDistinctKeywords >= 1")
    if cfg.idCardAspectMin > cfg.idCardAspectMax or cfg.portraitAspectMin > cfg.portraitAspectMax:
        problems.append("aspect ranges must have min <= max")
    if cfg.frequencyCap < 1:
        problems.append("frequencyCap must be >= 1")
    if cfg.comboFreeKeywords < 0 or cfg.formLabelLinesMin < 0 or cfg.idCardMaxLines < 0:
        problems.append("comboFreeKeywords, formLabelLinesMin and idCardMaxLines must be >= 0")
    if cfg.transactionRowsMin < 1 or cfg.transactionRowCap < 1:
        problems.append("transactionRowsMin and transactionRowCap must be >= 1")
    for name in ("frequencyBonus", "comboBonusPerKeyword", "cueGateRaw", "idCardAspectBonus", "idCardSparseBonus",
                 "portraitBonus", "formLabelBonus", "transactionRowBonus", "transactionHeaderBonus",
                 "rightAlignedAmountBonus"):
        if getattr(cfg, name) < 0:
            problems.append(f"{name} must be >= 0")
    for name in ("formLabelLineRatioMin", "rightAlignedRatioMin"):
        if not 0 <= getattr(cfg, name) <= 1:
            problems.append(f"{name} must be in [0, 1]")
    if problems:
        raise ClassifierConfigError("; ".join(problems))


def load_classifier_config(path: Union[str, Path, None] = None) -> ClassifierConfig:
    source = Path(path) if path else Path(os.environ.get("CLASSIFIER_CONFIG") or DEFAULT_PATH)
    if not source.is_absolute():
        source = SERVICE_ROOT / source
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClassifierConfigError(f"classifier config not found: {source.name}") from exc
    except (OSError, ValueError) as exc:
        raise ClassifierConfigError(f"classifier config unreadable: {source.name}") from exc
    if not isinstance(raw, dict):
        raise ClassifierConfigError("classifier config must be a JSON object")
    names = {f.name: f for f in fields(ClassifierConfig)}
    missing = sorted(set(names) - set(raw))
    unknown = sorted(set(raw) - set(names))
    if missing or unknown:
        raise ClassifierConfigError(f"classifier config keys: missing={missing} unknown={unknown}")
    values = {}
    for name, field in names.items():
        value = raw[name]
        kind = {"str": str, "int": int, "float": float}[field.type if isinstance(field.type, str) else field.type.__name__]
        if kind is str:
            ok = isinstance(value, str)
        elif kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok:
            raise ClassifierConfigError(f"classifier config '{name}' must be a {kind.__name__}")
        values[name] = kind(value)
    cfg = ClassifierConfig(**values)
    _validate(cfg)
    return cfg
