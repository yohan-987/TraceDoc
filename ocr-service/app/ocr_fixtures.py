"""Recorded OCR fixtures (contracts/fixtures/ocr/<docId>-page-N.json, each an OcrPage) for development and tests.

Fixtures are read-only inputs. ``seed_ocr`` puts them into an AnalysisContext exactly where live OCR would be cached,
so every analyzer reads them through the normal context interface and no OCR engine is needed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import REPO_ROOT
from .context import AnalysisContext
from .models import OcrPage

_NAME = re.compile(r"^(?P<doc>.+)-page-(?P<page>\d+)\.json$")


def fixtures_dir() -> Optional[Path]:
    """OCR_FIXTURES_DIR, else <repo>/contracts/fixtures/ocr; None if the folder does not exist."""
    raw = os.environ.get("OCR_FIXTURES_DIR")
    path = Path(raw) if raw else REPO_ROOT / "contracts" / "fixtures" / "ocr"
    return path if path.is_dir() else None


def list_fixtures(directory: Path) -> List[Tuple[str, int, Path]]:
    """(docId, 1-based page, path) for every <docId>-page-N.json, sorted."""
    found = []
    for path in directory.glob("*-page-*.json"):
        m = _NAME.match(path.name)
        if m:
            found.append((m.group("doc"), int(m.group("page")), path))
    return sorted(found)


def load_ocr_fixture(path: Path) -> OcrPage:
    return OcrPage.model_validate_json(path.read_text(encoding="utf-8"))


def seed_ocr(ctx: AnalysisContext, pages: Dict[int, OcrPage]) -> None:
    """Place recorded OcrPages (keyed by 1-based page number) into the context's OCR cache."""
    for number, page in pages.items():
        ctx.get_ocr_page(number, lambda p=page: p)
