"""Loading and validating the extraction schemas (app/schemas/*.yaml) and shared settings (_settings.yaml).

A missing or malformed file raises SchemaError; the analyzer turns that into an ERROR status for the affected
pages, never a crash. FIELD_SCHEMAS_DIR overrides the schema folder.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import yaml

from .config import SERVICE_ROOT
from .extract_normalize import NORMALIZERS, VALIDATORS, resolve_pattern

SCHEMAS_DIR = SERVICE_ROOT / "app" / "schemas"
SUPPORTED_DOC_TYPES = ("id_card", "application_form", "bank_statement")
KINDS = ("anchor", "composite", "anchor_region")
_GEOMETRY_DEFAULTS = {"columnLeftTolerance": 0.02}
_SCORING_KEYS = ("regionMatchBonus", "regionMissPenalty", "multipleCandidatesPenalty", "patternMissPenalty",
                 "rightPlacementPenalty", "belowPlacementPenalty", "continuationPenalty")


class SchemaError(Exception):
    """A schema or settings file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Strategy:
    kind: str
    confidence: float
    anchors: Tuple[str, ...] = ()
    pattern: Optional[str] = None            # regex (aliases resolved)
    pick: str = "first"
    parts: Tuple[Tuple[str, ...], ...] = ()  # composite: one anchor list per part
    placement: str = "right"                 # anchor_region
    width: float = 0.3
    height: float = 0.05


@dataclass(frozen=True)
class FieldSpec:
    name: str
    semanticType: str
    normalizer: str
    validators: Tuple[Tuple[str, Mapping[str, Any]], ...]
    strategies: Tuple[Strategy, ...]
    region: Optional[Tuple[float, float, float, float]] = None   # expected region [x, y, w, h], 0..1
    continuationLines: int = 0


@dataclass(frozen=True)
class DocSchema:
    docType: str
    schemaVersion: str
    fields: Tuple[FieldSpec, ...]
    labels: Tuple[str, ...] = ()   # printed labels / headings of this document type that are not extracted fields (regexes)


@dataclass(frozen=True)
class ExtractorSettings:
    settingsVersion: str
    lowOcrConfidence: float
    lowExtractionConfidence: float
    dateOrder: Optional[str]
    scoring: Mapping[str, float]
    labelVocabulary: Tuple[str, ...] = ()               # form words shared by every document type (regexes)
    geometry: Mapping[str, float] = None                # label-to-value geometry (page fractions); see _settings.yaml


def schemas_dir() -> Path:
    raw = os.environ.get("FIELD_SCHEMAS_DIR")
    path = Path(raw) if raw else SCHEMAS_DIR
    return path if path.is_absolute() else SERVICE_ROOT / path


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"schema file not found: {path.name}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise SchemaError(f"schema file unreadable: {path.name}") from exc
    if not isinstance(data, dict):
        raise SchemaError(f"{path.name}: top level must be a mapping")
    return data


def _num(value: Any, where: str, lo: float = 0.0, hi: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise SchemaError(f"{where} must be a number between {lo:g} and {hi:g}")
    return float(value)


def _regex(pattern: Any, where: str) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise SchemaError(f"{where} must be a non-empty regex string")
    try:
        re.compile(resolve_pattern(pattern))
    except re.error as exc:
        raise SchemaError(f"{where} is not a valid regex ({exc})") from exc
    return pattern


def _anchors(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SchemaError(f"{where} must be a non-empty list of anchor regexes")
    return tuple(_regex(a, f"{where}[{i}]") for i, a in enumerate(value))


def _strategy(raw: Any, where: str) -> Strategy:
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        raise SchemaError(f"{where}.kind must be one of {KINDS}")
    kind = raw["kind"]
    confidence = _num(raw.get("confidence"), f"{where}.confidence")
    pattern = _regex(raw["pattern"], f"{where}.pattern") if raw.get("pattern") is not None else None
    pick = raw.get("pick", "first")
    if pick not in ("first", "last"):
        raise SchemaError(f"{where}.pick must be 'first' or 'last'")
    if kind == "composite":
        parts = raw.get("parts")
        if not isinstance(parts, list) or len(parts) < 2:
            raise SchemaError(f"{where}.parts must list at least two anchor lists")
        return Strategy(kind, confidence, pattern=pattern, pick=pick,
                        parts=tuple(_anchors(p, f"{where}.parts[{i}]") for i, p in enumerate(parts)))
    anchors = _anchors(raw.get("anchors"), f"{where}.anchors")
    if kind == "anchor_region":
        placement = raw.get("placement", "right")
        if placement not in ("right", "below"):
            raise SchemaError(f"{where}.placement must be 'right' or 'below'")
        return Strategy(kind, confidence, anchors=anchors, placement=placement,
                        width=_num(raw.get("width", 0.3), f"{where}.width"),
                        height=_num(raw.get("height", 0.05), f"{where}.height"))
    return Strategy(kind, confidence, anchors=anchors, pattern=pattern, pick=pick)


def _labels(value: Any, where: str) -> Tuple[str, ...]:
    """Optional list of label regexes (printed words that are never a field value). May be empty."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SchemaError(f"{where} must be a list of regexes")
    return tuple(_regex(v, f"{where}[{i}]") for i, v in enumerate(value))


def _validators(raw: Any, where: str) -> Tuple[Tuple[str, Mapping[str, Any]], ...]:
    if not isinstance(raw, list):
        raise SchemaError(f"{where} must be a list")
    out = []
    for i, item in enumerate(raw):
        name, params = (item, {}) if isinstance(item, str) else (item.get("name"), item) if isinstance(item, dict) else (None, {})
        if name not in VALIDATORS:
            raise SchemaError(f"{where}[{i}] must name one of {VALIDATORS}")
        if name == "regex":
            _regex(params.get("pattern"), f"{where}[{i}].pattern")
        out.append((name, dict(params)))
    return tuple(out)


def _field(raw: Any, where: str) -> FieldSpec:
    if not isinstance(raw, dict):
        raise SchemaError(f"{where} must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise SchemaError(f"{where}.name must be a snake_case string")
    where = f"{where}({name})"
    semantic = raw.get("semanticType")
    if not isinstance(semantic, str) or not semantic:
        raise SchemaError(f"{where}.semanticType is required")
    normalizer = raw.get("normalizer", "text")
    if normalizer not in NORMALIZERS:
        raise SchemaError(f"{where}.normalizer must be one of {NORMALIZERS}")
    strategies = raw.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        raise SchemaError(f"{where}.strategies must be a non-empty list")
    region = raw.get("region")
    if region is not None:
        if not isinstance(region, list) or len(region) != 4:
            raise SchemaError(f"{where}.region must be [x, y, w, h]")
        region = tuple(_num(v, f"{where}.region") for v in region)
    cont = raw.get("continuationLines", 0)
    if isinstance(cont, bool) or not isinstance(cont, int) or not 0 <= cont <= 10:
        raise SchemaError(f"{where}.continuationLines must be an integer 0-10")
    return FieldSpec(name, semantic, normalizer, _validators(raw.get("validators", []), f"{where}.validators"),
                     tuple(_strategy(s, f"{where}.strategies[{i}]") for i, s in enumerate(strategies)),
                     region, cont)


def load_schema(doc_type: str, directory: Optional[Path] = None) -> DocSchema:
    if doc_type not in SUPPORTED_DOC_TYPES:
        raise SchemaError(f"no extraction schema for docType {doc_type!r}")
    path = (directory or schemas_dir()) / f"{doc_type}.yaml"
    data = _read_yaml(path)
    if data.get("docType") != doc_type:
        raise SchemaError(f"{path.name}: docType must be {doc_type!r}")
    version = data.get("schemaVersion")
    if not isinstance(version, str) or not version:
        raise SchemaError(f"{path.name}: schemaVersion (string) is required")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError(f"{path.name}: fields must be a non-empty list")
    fields = tuple(_field(f, f"{path.name}: fields[{i}]") for i, f in enumerate(raw_fields))
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise SchemaError(f"{path.name}: duplicate field names")
    return DocSchema(doc_type, version, fields, _labels(data.get("labels"), f"{path.name}: labels"))


def load_settings(directory: Optional[Path] = None) -> ExtractorSettings:
    path = (directory or schemas_dir()) / "_settings.yaml"
    data = _read_yaml(path)
    scoring = data.get("scoring")
    if not isinstance(scoring, dict) or any(k not in scoring for k in _SCORING_KEYS):
        raise SchemaError(f"{path.name}: scoring must define {list(_SCORING_KEYS)}")
    order = data.get("dateOrder")
    if order not in ("DMY", "MDY", None):
        raise SchemaError(f"{path.name}: dateOrder must be DMY, MDY or null")
    version = data.get("settingsVersion")
    if not isinstance(version, str) or not version:
        raise SchemaError(f"{path.name}: settingsVersion (string) is required")
    geometry = dict(_GEOMETRY_DEFAULTS)
    raw_geometry = data.get("geometry")
    if raw_geometry is not None:
        if not isinstance(raw_geometry, dict) or any(k not in _GEOMETRY_DEFAULTS for k in raw_geometry):
            raise SchemaError(f"{path.name}: geometry may only define {list(_GEOMETRY_DEFAULTS)}")
        geometry.update({k: _num(v, f"geometry.{k}", 0.0, 0.25) for k, v in raw_geometry.items()})
    return ExtractorSettings(
        version, _num(data.get("lowOcrConfidence"), "lowOcrConfidence"),
        _num(data.get("lowExtractionConfidence"), "lowExtractionConfidence"), order,
        {k: _num(scoring[k], f"scoring.{k}") for k in _SCORING_KEYS},
        _labels(data.get("labelVocabulary"), f"{path.name}: labelVocabulary"), geometry)
