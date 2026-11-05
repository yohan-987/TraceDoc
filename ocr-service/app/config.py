"""Runtime configuration and shared-contract import path.

Settings come from environment variables (optionally pre-loaded from ocr-service/.env).
This module also puts the repo's ``contracts/`` folder on ``sys.path`` (read-only) so that
``from analyze_models import AnalyzeResponse`` works, as described in contracts/README.md.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]  # ocr-service/
REPO_ROOT = SERVICE_ROOT.parent
PIPELINE_VERSION = "0.1.0"


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE). Real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_contracts_on_path() -> Path:
    """Add contracts/ to sys.path. Never writes to it."""
    raw = os.environ.get("CONTRACTS_DIR")
    contracts = Path(raw) if raw else REPO_ROOT / "contracts"
    if not contracts.is_absolute():
        contracts = SERVICE_ROOT / contracts
    contracts = contracts.resolve()
    if (contracts / "analyze_models.py").is_file() and str(contracts) not in sys.path:
        sys.path.insert(0, str(contracts))
    return contracts


ensure_contracts_on_path()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    render_dpi: int = 150
    analyzer_timeout_s: float = 30.0
    max_pages: int = 100
    pipeline_version: str = PIPELINE_VERSION

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(SERVICE_ROOT / ".env")
        raw_dir = Path(os.environ.get("DATA_DIR", "../data"))
        data_dir = (raw_dir if raw_dir.is_absolute() else SERVICE_ROOT / raw_dir).resolve()
        dpi = int(os.environ.get("RENDER_DPI", "150"))
        timeout = float(os.environ.get("ANALYZER_TIMEOUT_S", "30"))
        max_pages = int(os.environ.get("MAX_PAGES", "100"))
        if not 72 <= dpi <= 600:
            raise ValueError("RENDER_DPI must be between 72 and 600")
        if timeout <= 0:
            raise ValueError("ANALYZER_TIMEOUT_S must be > 0")
        if max_pages < 1:
            raise ValueError("MAX_PAGES must be >= 1")
        return cls(data_dir=data_dir, render_dpi=dpi, analyzer_timeout_s=timeout, max_pages=max_pages)
