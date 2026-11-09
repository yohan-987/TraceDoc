"""Layout/table thresholds and header vocabulary: config/layout.json (override with LAYOUT_CONFIG).

The JSON file is the single source of truth (no defaults in code); every value is echoed into provenance.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Tuple, Union

from .config import SERVICE_ROOT

DEFAULT_PATH = SERVICE_ROOT / "config" / "layout.json"
_LISTS = ("headerDate", "headerDescription", "headerDebit", "headerCredit", "headerBalance", "totalsKeywords",
          "datePatterns")


class LayoutConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class LayoutConfig:
    configVersion: str
    rowYToleranceFactor: float          # words share a row when their centres differ by < factor * median word height
    skewMaxDeg: float                   # largest page tilt compensated when grouping rows (0 disables)
    skewStepDeg: float
    minHeaderRoles: int                 # distinct header roles needed to call a row a table header
    headerDate: Tuple[str, ...]
    headerDescription: Tuple[str, ...]
    headerDebit: Tuple[str, ...]
    headerCredit: Tuple[str, ...]
    headerBalance: Tuple[str, ...]
    totalsKeywords: Tuple[str, ...]
    datePatterns: Tuple[str, ...]
    amountColumnMaxDistance: float      # an amount farther than this (page width fraction) from every column is unassigned
    confidenceHeaderOnPage: float
    confidenceInheritedColumns: float
    confidenceAmbiguityPenalty: float

    def as_provenance(self) -> Dict[str, Union[float, int, str]]:
        out: Dict[str, Union[float, int, str]] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f"cfg.{f.name}"] = "|".join(v) if isinstance(v, tuple) else v
        return out


def _validate(cfg: LayoutConfig) -> None:
    problems = []
    for f in fields(cfg):
        v = getattr(cfg, f.name)
        if isinstance(v, float) and not math.isfinite(v):
            problems.append(f"{f.name} must be finite")
    if not 0 < cfg.rowYToleranceFactor <= 2:
        problems.append("rowYToleranceFactor must be in (0, 2]")
    if not 0 <= cfg.skewMaxDeg <= 10 or not 0.01 <= cfg.skewStepDeg <= 1:
        problems.append("skewMaxDeg must be in [0, 10] and skewStepDeg in [0.01, 1]")
    if not 2 <= cfg.minHeaderRoles <= 5:
        problems.append("minHeaderRoles must be in [2, 5]")
    if not 0 < cfg.amountColumnMaxDistance <= 1:
        problems.append("amountColumnMaxDistance must be in (0, 1]")
    for name in ("confidenceHeaderOnPage", "confidenceInheritedColumns", "confidenceAmbiguityPenalty"):
        if not 0 <= getattr(cfg, name) <= 1:
            problems.append(f"{name} must be in [0, 1]")
    for name in _LISTS:
        if not getattr(cfg, name):
            problems.append(f"{name} must not be empty")
    for pat in cfg.datePatterns:
        try:
            re.compile(pat)
        except re.error:
            problems.append(f"datePatterns has an invalid regular expression: {pat}")
    if problems:
        raise LayoutConfigError("; ".join(problems))


def load_layout_config(path: Union[str, Path, None] = None) -> LayoutConfig:
    source = Path(path) if path else Path(os.environ.get("LAYOUT_CONFIG") or DEFAULT_PATH)
    if not source.is_absolute():
        source = SERVICE_ROOT / source
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LayoutConfigError(f"layout config not found: {source.name}") from exc
    except (OSError, ValueError) as exc:
        raise LayoutConfigError(f"layout config unreadable: {source.name}") from exc
    if not isinstance(raw, dict):
        raise LayoutConfigError("layout config must be a JSON object")
    names = [f.name for f in fields(LayoutConfig)]
    missing, unknown = sorted(set(names) - set(raw)), sorted(set(raw) - set(names))
    if missing or unknown:
        raise LayoutConfigError(f"layout config keys: missing={missing} unknown={unknown}")
    values: Dict[str, object] = {}
    for name in names:
        v = raw[name]
        if name in _LISTS:
            if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
                raise LayoutConfigError(f"layout config '{name}' must be a list of non-empty strings")
            values[name] = tuple(s.lower() if name.startswith(("header", "totals")) else s for s in v)
        elif name == "configVersion":
            if not isinstance(v, str):
                raise LayoutConfigError("layout config 'configVersion' must be a string")
            values[name] = v
        elif name == "minHeaderRoles":
            if not isinstance(v, int) or isinstance(v, bool):
                raise LayoutConfigError("layout config 'minHeaderRoles' must be an int")
            values[name] = v
        else:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise LayoutConfigError(f"layout config '{name}' must be a number")
            values[name] = float(v)
    cfg = LayoutConfig(**values)  # type: ignore[arg-type]
    _validate(cfg)
    return cfg
