"""Image-forensics thresholds: loaded from config/image_forensics.json (override with IMAGE_FORENSICS_CONFIG).

The JSON file is the single source of truth (no defaults in code). Every value is a plain number or string, so the
whole configuration is echoed verbatim into provenance (``cfg.<name>`` keys): a reviewer can see exactly which
thresholds produced a signal.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, Union

from .config import SERVICE_ROOT

DEFAULT_PATH = SERVICE_ROOT / "config" / "image_forensics.json"


class ImageForensicsConfigError(Exception):
    """The configuration file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class ImageForensicsConfig:
    configVersion: str
    maxWorkingSidePx: int            # larger images are downscaled to this long side before analysis
    elaQuality: int                  # JPEG re-save quality for error-level analysis
    elaBlockPx: int
    elaActivePercentile: float       # only blocks with edge energy above this percentile are scored
    elaZThreshold: float             # robust z of the residual/edge ratio that marks a block
    elaMinBlocks: int                # marked blocks needed for a region
    noiseBlockPx: int
    noiseBackgroundMinLevel: int     # a block is "paper" when its darkest pixel is at least this bright
    noiseHighPassSigma: float
    noiseLowZThreshold: float        # blocks quieter than the page median by this many robust sigmas
    noiseMinBlocks: int
    noiseMinBackgroundBlocks: int    # fewer paper blocks than this: the test is not informative
    noiseMinMedianSd: float          # a page with (nearly) no noise at all cannot be tested
    noiseMinMad: float
    cmOrbFeatures: int
    cmRatio: float                   # Lowe ratio between the best and second-best non-self match
    cmMaxDescriptorDistance: int
    cmMinOffsetPx: int               # matches closer than this are the same feature, not a copy
    cmOffsetBinPx: int
    cmMinVotes: int                  # matches that must agree on one offset
    cmMaxCandidates: int
    cmRefinePx: int
    cmPixelTolerance: int            # grey-level tolerance for "identical pixels"
    cmInkLevel: int                  # pixels darker than this count as ink
    cmMergeGapFraction: float        # duplicated pieces of one text line closer than this (of width) are merged
    cmMinMatchedInkFraction: float   # duplicated ink must reach this share of the page area ("tiny matches" ignored)
    cmMinCoverage: float             # share of the text line's ink that is duplicated (isolated repeated words fail)
    cmMinGlyphComponents: int
    cmPeriodicCoverage: float        # also duplicated at twice the offset: a regular pattern, not a one-off copy

    def as_provenance(self) -> Dict[str, Union[float, int, str]]:
        return {f"cfg.{f.name}": getattr(self, f.name) for f in fields(self)}


def _validate(cfg: ImageForensicsConfig) -> None:
    problems = []
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{f.name} must be a finite number")
    if not 256 <= cfg.maxWorkingSidePx <= 8000:
        problems.append("maxWorkingSidePx must be in [256, 8000]")
    if not 1 <= cfg.elaQuality <= 100:
        problems.append("elaQuality must be in [1, 100]")
    for name in ("elaBlockPx", "noiseBlockPx", "elaMinBlocks", "noiseMinBlocks", "noiseMinBackgroundBlocks",
                 "cmOrbFeatures", "cmMinOffsetPx", "cmOffsetBinPx", "cmMinVotes", "cmMaxCandidates",
                 "cmMinGlyphComponents"):
        if getattr(cfg, name) < 1:
            problems.append(f"{name} must be >= 1")
    if not 0 <= cfg.elaActivePercentile < 100:
        problems.append("elaActivePercentile must be in [0, 100)")
    if not 0 < cfg.cmRatio <= 1:
        problems.append("cmRatio must be in (0, 1]")
    if not 0 < cfg.cmMergeGapFraction <= 1:
        problems.append("cmMergeGapFraction must be in (0, 1]")
    if not 0 < cfg.cmMinCoverage <= 1 or not 0 < cfg.cmPeriodicCoverage <= 1:
        problems.append("cmMinCoverage and cmPeriodicCoverage must be in (0, 1]")
    if not 0 < cfg.cmMinMatchedInkFraction < 1:
        problems.append("cmMinMatchedInkFraction must be in (0, 1)")
    if not 0 <= cfg.cmRefinePx <= 16:
        problems.append("cmRefinePx must be in [0, 16]")
    if not 0 < cfg.cmPixelTolerance <= 255 or not 0 < cfg.cmInkLevel <= 255:
        problems.append("cmPixelTolerance and cmInkLevel must be in (0, 255]")
    if cfg.noiseLowZThreshold <= 0 or cfg.elaZThreshold <= 0:
        problems.append("z thresholds must be > 0")
    if cfg.noiseMinMedianSd < 0 or cfg.noiseMinMad < 0:
        problems.append("noise minimums must be >= 0")
    if problems:
        raise ImageForensicsConfigError("; ".join(problems))


def load_image_forensics_config(path: Union[str, Path, None] = None) -> ImageForensicsConfig:
    source = Path(path) if path else Path(os.environ.get("IMAGE_FORENSICS_CONFIG") or DEFAULT_PATH)
    if not source.is_absolute():
        source = SERVICE_ROOT / source
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImageForensicsConfigError(f"image forensics config not found: {source.name}") from exc
    except (OSError, ValueError) as exc:
        raise ImageForensicsConfigError(f"image forensics config unreadable: {source.name}") from exc
    if not isinstance(raw, dict):
        raise ImageForensicsConfigError("image forensics config must be a JSON object")
    names = {f.name: f for f in fields(ImageForensicsConfig)}
    missing = sorted(set(names) - set(raw))
    unknown = sorted(set(raw) - set(names))
    if missing or unknown:
        raise ImageForensicsConfigError(f"image forensics config keys: missing={missing} unknown={unknown}")
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
            raise ImageForensicsConfigError(f"image forensics config '{name}' must be a {kind.__name__}")
        values[name] = kind(value)
    cfg = ImageForensicsConfig(**values)
    _validate(cfg)
    return cfg
