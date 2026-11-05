"""Models used by the service.

All wire-format models are imported from the shared contract (contracts/analyze_models.py) and
re-exported here. The only models defined locally are:
  * AnalyzeRequest  - the contract defines no request shape, only {docId};
  * internal dataclasses (never serialized): RenderedPage, RenderedDocument, AnalyzerResult.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import config  # noqa: F401  (puts contracts/ on sys.path)

try:
    from analyze_models import (  # type: ignore[import-not-found]
        AnalyzeResponse,
        AnalyzerStatus,
        ExtractedField,
        ForensicSignal,
        OcrPage,
        PageRecord,
        Provenance,
        Scalar,
    )
except ImportError as exc:  # pragma: no cover - environment problem
    raise RuntimeError(
        "Cannot import contracts/analyze_models.py. Keep ocr-service/ next to contracts/, or set "
        "CONTRACTS_DIR to the contracts folder."
    ) from exc

__all__ = [
    "AnalyzeRequest", "AnalyzeResponse", "AnalyzerResult", "AnalyzerStatus", "DOC_ID_PATTERN",
    "ExtractedField", "ForensicSignal", "InvalidDocIdError", "OcrPage", "PageRecord", "Provenance",
    "RenderedDocument", "RenderedPage", "Scalar", "validate_doc_id",
]

DOC_ID_PATTERN = r"[A-Za-z0-9_-]+"
_DOC_ID_RE = re.compile(DOC_ID_PATTERN)


class InvalidDocIdError(ValueError):
    pass


def validate_doc_id(doc_id: str) -> str:
    """Defense in depth: the API model and the renderer both enforce this."""
    if not isinstance(doc_id, str) or _DOC_ID_RE.fullmatch(doc_id) is None:
        raise InvalidDocIdError("docId must match [A-Za-z0-9_-]+")
    return doc_id


class AnalyzeRequest(BaseModel):
    """POST /analyze body. Document IDs only; unknown keys (e.g. a path) are rejected."""

    model_config = ConfigDict(extra="forbid")
    docId: str = Field(pattern=rf"^{DOC_ID_PATTERN}$")


@dataclass(frozen=True)
class RenderedPage:
    record: PageRecord          # contract record (relative renderPath, dpi, rotation, deskew)
    path: Path                  # absolute path of the canonical PNG
    width_px: int
    height_px: int

    @property
    def page(self) -> int:
        return self.record.page


@dataclass(frozen=True)
class RenderedDocument:
    doc_id: str
    source_path: Path
    input_sha256: str           # SHA-256 of the original file
    pages: List[RenderedPage]


@dataclass
class AnalyzerResult:
    """What an analyzer returns. Only AVAILABLE results contribute fields/signals."""

    status: AnalyzerStatus = "AVAILABLE"
    reason: Optional[str] = None
    fields: List[ExtractedField] = field(default_factory=list)
    signals: List[ForensicSignal] = field(default_factory=list)
    model_version: Optional[str] = None
    config: Dict[str, Scalar] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, reason: str) -> "AnalyzerResult":
        return cls(status="UNAVAILABLE", reason=reason)

    @classmethod
    def not_applicable(cls, reason: str) -> "AnalyzerResult":
        return cls(status="NOT_APPLICABLE", reason=reason)
