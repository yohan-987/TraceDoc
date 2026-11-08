"""Per-request analysis context shared by all analyzers."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

from .config import Settings
from .models import OcrPage, RenderedDocument, RenderedPage


class AnalysisContext:
    """Holds the rendered document and a compute-once cache.

    OCR (O2) is computed by whichever analyzer needs it first via ``get_ocr_page``; later analyzers
    get the cached result instead of recomputing. The cache is request-scoped.
    """

    def __init__(self, document: RenderedDocument, settings: Settings) -> None:
        self.document = document
        self.settings = settings
        self._cache: Dict[Any, Any] = {}
        self._lock = threading.Lock()                   # guards the two dicts only; never held while computing
        self._key_locks: Dict[Any, threading.Lock] = {}  # one lock per key: compute-once without blocking other keys

    @property
    def doc_id(self) -> str:
        return self.document.doc_id

    @property
    def pages(self) -> List[RenderedPage]:
        return self.document.pages

    @property
    def input_sha256(self) -> str:
        return self.document.input_sha256

    def get_or_compute(self, key: Any, factory: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``, computing it once with ``factory``."""
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:  # a hung factory only blocks callers that need this same key
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
            value = factory()  # if this raises nothing is cached and the next caller retries
            with self._lock:
                self._cache[key] = value
            return value

    def get_ocr_page(self, page: int, compute: Callable[[], OcrPage]) -> OcrPage:
        return self.get_or_compute(("ocr", page), compute)
