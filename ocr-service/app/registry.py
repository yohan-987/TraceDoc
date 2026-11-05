"""Analyzer plugin registry and runner.

Writing a plugin: add a module under app/analyzers/ and decorate a class with @register_analyzer.
The service imports every module in that package at startup (see ``discover_analyzers``).
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, ClassVar, Dict, List, Optional, Type, Union, get_args

from .context import AnalysisContext
from .models import (
    AnalyzerResult,
    AnalyzerStatus,
    ExtractedField,
    ForensicSignal,
    Provenance,
    RenderedPage,
)

logger = logging.getLogger(__name__)
_VALID_STATUSES = set(get_args(AnalyzerStatus))
_MAX_REASON = 200


class Analyzer(ABC):
    name: ClassVar[str]
    version: ClassVar[str]

    def applies_to(self, page: RenderedPage) -> bool:
        return True

    @abstractmethod
    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        """Analyze ctx.document. Share expensive work (OCR) via ctx.get_or_compute / get_ocr_page."""


class AnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: Dict[str, Analyzer] = {}
        self.load_errors: Dict[str, str] = {}  # plugin modules that failed to import

    def register(self, analyzer: Union[Analyzer, Type[Analyzer]]) -> Analyzer:
        instance = analyzer() if isinstance(analyzer, type) else analyzer
        name = getattr(instance, "name", None)
        version = getattr(instance, "version", None)
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("an analyzer needs non-empty string 'name' and 'version'")
        if name in self._analyzers:
            raise ValueError(f"analyzer '{name}' is already registered")
        self._analyzers[name] = instance
        return instance

    def analyzers(self) -> List[Analyzer]:
        return [self._analyzers[k] for k in sorted(self._analyzers)]

    def __len__(self) -> int:
        return len(self._analyzers)


default_registry = AnalyzerRegistry()


def register_analyzer(cls: Type[Analyzer]) -> Type[Analyzer]:
    """Class decorator: the analyzer registers itself in the default registry on import."""
    default_registry.register(cls)
    return cls


def discover_analyzers(package: str = "app.analyzers") -> AnalyzerRegistry:
    """Import every plugin module so its @register_analyzer decorators run.

    An import failure is recorded and reported as ERROR in the response, never silently dropped.
    """
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        full = f"{package}.{info.name}"
        try:
            importlib.import_module(full)
        except Exception as exc:
            logger.exception("failed to import analyzer module %s", full)
            default_registry.load_errors[full] = _reason(exc)
    return default_registry


@dataclass
class RunOutcome:
    statuses: Dict[str, AnalyzerStatus] = field(default_factory=dict)
    fields: List[ExtractedField] = field(default_factory=list)
    signals: List[ForensicSignal] = field(default_factory=list)
    provenance: List[Provenance] = field(default_factory=list)


class _Timeout(Exception):
    pass


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_REASON]


def _call_with_timeout(fn: Callable[[], AnalyzerResult], timeout_s: float, label: str) -> AnalyzerResult:
    """Run ``fn`` in a daemon thread. A Python thread cannot be killed, so a timed-out analyzer is
    abandoned (its result is discarded) and may keep running in the background until it returns."""
    box: Dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - nothing may escape the plugin boundary
            box["error"] = exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__)

    thread = threading.Thread(target=target, name=f"analyzer-{label}", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise _Timeout(f"timed out after {timeout_s:g}s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provenance(name: str, version: str, status: AnalyzerStatus, ctx: AnalysisContext,
                reason: Optional[str], result: Optional[AnalyzerResult]) -> Provenance:
    config = dict(result.config) if result is not None else {}
    if reason:
        config["reason"] = reason
    extra = {}
    if result is not None and result.model_version is not None:
        extra["modelVersion"] = result.model_version  # omit when absent: the schema has no null here
    return Provenance(
        analyzer=name, analyzerVersion=version, config=config, inputSha256=ctx.input_sha256,
        processedAt=_now(), status=status, **extra,
    )


def _execute(analyzer: Analyzer, ctx: AnalysisContext) -> AnalyzerResult:
    if not any(analyzer.applies_to(page) for page in ctx.pages):
        return AnalyzerResult.not_applicable("applies_to() is false for every page")
    return analyzer.run(ctx)


def _check(result: object) -> Optional[str]:
    """Return a problem description if the analyzer returned something unusable."""
    if not isinstance(result, AnalyzerResult):
        return "analyzer did not return an AnalyzerResult"
    if result.status not in _VALID_STATUSES:
        return f"invalid status {result.status!r}"
    if not all(isinstance(f, ExtractedField) for f in result.fields):
        return "fields must be ExtractedField instances"
    if not all(isinstance(s, ForensicSignal) for s in result.signals):
        return "signals must be ForensicSignal instances"
    return None


def run_analyzers(ctx: AnalysisContext, registry: AnalyzerRegistry, timeout_s: float) -> RunOutcome:
    """Run every registered analyzer in name order. One analyzer's failure never affects another's
    output. Only AVAILABLE results contribute fields/signals; any other status is reported as-is."""
    outcome = RunOutcome()

    for module, reason in sorted(registry.load_errors.items()):
        outcome.statuses[module] = "ERROR"
        outcome.provenance.append(_provenance(module, "unknown", "ERROR", ctx, f"import failed: {reason}", None))

    for analyzer in registry.analyzers():
        started = time.monotonic()
        status: AnalyzerStatus = "ERROR"
        reason: Optional[str] = None
        result: Optional[AnalyzerResult] = None
        try:
            raw = _call_with_timeout(lambda a=analyzer: _execute(a, ctx), timeout_s, analyzer.name)
            problem = _check(raw)
            if problem:
                reason = problem
            else:
                result, status, reason = raw, raw.status, raw.reason
                if status == "AVAILABLE":
                    outcome.fields.extend(raw.fields)
                    outcome.signals.extend(raw.signals)
        except _Timeout as exc:
            reason = str(exc)
            logger.error("analyzer %s %s", analyzer.name, reason)
        except Exception as exc:  # noqa: BLE001
            reason = _reason(exc)
            logger.exception("analyzer %s failed", analyzer.name)
        outcome.statuses[analyzer.name] = status
        outcome.provenance.append(_provenance(analyzer.name, analyzer.version, status, ctx, reason, result))
        logger.info("analyzer %s -> %s in %.2fs", analyzer.name, status, time.monotonic() - started)
    return outcome
