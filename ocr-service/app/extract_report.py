"""Compare extractor output with expected values and print readable accuracy tables.

The expected values come ONLY from ``samples/manifest.json`` (SAMPLES_MANIFEST overrides the path); nothing here
invents expected values or writes a replacement manifest. If the manifest, or the recorded OCR for a document, is
missing, the affected comparisons are listed as "cannot be performed" and are not counted as matches or mismatches.

Manifest shape accepted (ASSUMPTION: the real shape is defined by the samples owner, see the README):
  a list of documents, or an object with a "documents" list (or a docId -> document mapping); each document has
  "docId" (or "id", else the file stem), optional "docType", and "fields": either {name: value} or
  {name: {"normalizedValue"|"value"|"rawValue": ..., "bbox": [x,y,w,h], "page": n}} or a list of such objects with "name".
Comparison is exact string equality: "normalizedValue" against the extracted normalizedValue, "rawValue" against the
extracted rawValue, and a plain "value" against normalizedValue (rawValue only if the extractor produced none).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import REPO_ROOT

BBox = Tuple[float, float, float, float]


@dataclass(frozen=True)
class Expected:
    value: str
    kind: str                       # normalizedValue | rawValue | value
    bbox: Optional[BBox] = None
    page: Optional[int] = None


@dataclass(frozen=True)
class ManifestDoc:
    docId: str
    docType: Optional[str]
    fields: Dict[str, Expected]


@dataclass
class Manifest:
    docs: Dict[str, ManifestDoc] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Row:
    docType: str
    docId: str
    name: str
    expected: str
    extracted: str
    raw: str
    match: bool
    iou: Optional[float] = None


@dataclass
class Comparison:
    rows: List[Row] = field(default_factory=list)
    unexpected: List[Tuple[str, str, str, str]] = field(default_factory=list)  # docType, docId, field, value
    cannot_compare: List[str] = field(default_factory=list)


def find_manifest() -> Optional[Path]:
    raw = os.environ.get("SAMPLES_MANIFEST")
    candidates = [Path(raw)] if raw else [REPO_ROOT / "samples" / "manifest.json",
                                          REPO_ROOT / "samples" / "dev" / "manifest.json"]
    return next((c for c in candidates if c.is_file()), None)


def _expected(raw: Any) -> Optional[Expected]:
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        return Expected(str(raw), "value")
    if not isinstance(raw, dict):
        return None
    for kind in ("normalizedValue", "value", "rawValue"):
        if raw.get(kind) is not None:
            bbox = raw.get("bbox")
            ok = isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox)
            page = raw.get("page") if isinstance(raw.get("page"), int) else None
            return Expected(str(raw[kind]), kind, tuple(float(v) for v in bbox) if ok else None, page)
    return None


def load_manifest(path: Path) -> Manifest:
    manifest = Manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        manifest.problems.append(f"{path.name}: unreadable ({type(exc).__name__})")
        return manifest
    if isinstance(data, dict):
        for key in ("documents", "files", "docs", "entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [{"docId": k, **v} for k, v in data.items() if isinstance(v, dict)]
    if not isinstance(data, list):
        manifest.problems.append(f"{path.name}: expected a list of documents")
        return manifest
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            manifest.problems.append(f"entry {i}: not an object")
            continue
        doc_id = entry.get("docId") or entry.get("id") or (Path(str(entry["file"])).stem if entry.get("file") else None)
        raw_fields = entry.get("fields")
        if not doc_id or raw_fields is None:
            manifest.problems.append(f"entry {i}: needs a docId and a 'fields' value")
            continue
        items = raw_fields.items() if isinstance(raw_fields, dict) else \
            [(f.get("name"), f) for f in raw_fields if isinstance(f, dict)]
        fields: Dict[str, Expected] = {}
        for name, raw in items:
            exp = _expected(raw)
            if name and exp is not None:
                fields[str(name)] = exp
            else:
                manifest.problems.append(f"{doc_id}: field {name!r} has no expected value")
        manifest.docs[str(doc_id)] = ManifestDoc(str(doc_id), entry.get("docType"), fields)
    return manifest


def _iou(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def compare(manifest: Manifest, extracted: Dict[str, Sequence[Any]], doc_types: Dict[str, str]) -> Comparison:
    """``extracted``: docId -> ExtractedField list (None/absent = no OCR available for that document)."""
    out = Comparison()
    for doc_id, doc in sorted(manifest.docs.items()):
        if doc_id not in extracted:
            out.cannot_compare.append(f"{doc_id}: no recorded OCR / extraction available, "
                                      f"{len(doc.fields)} expected field(s) not compared")
            continue
        found = extracted[doc_id]
        doc_type = doc.docType or doc_types.get(doc_id, "?")
        used = set()
        for name, exp in sorted(doc.fields.items()):
            hit = next((f for f in found if f.name == name and (exp.page is None or f.region.page == exp.page)), None)
            if hit is None:
                out.rows.append(Row(doc_type, doc_id, name, exp.value, "<missing>", "", False))
                continue
            used.add(hit.fieldId)
            if exp.kind == "rawValue":
                actual = hit.rawValue
            else:
                actual = hit.normalizedValue if hit.normalizedValue is not None else hit.rawValue
            iou = _iou(tuple(hit.region.bbox), exp.bbox) if exp.bbox else None
            out.rows.append(Row(doc_type, doc_id, name, exp.value, actual, hit.rawValue, actual == exp.value, iou))
        for f in found:
            if f.fieldId not in used and f.name not in doc.fields:
                out.unexpected.append((doc_type, doc_id, f.name, f.normalizedValue or f.rawValue))
    return out


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    rows = [list(map(str, r)) for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = lambda cells: " | ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), "-+-".join("-" * w for w in widths)] + [line(r) for r in rows])


def format_rows(cmp: Comparison) -> str:
    rows = [(r.docType, r.docId, r.name, r.expected, r.extracted, "OK" if r.match else "MISMATCH") for r in cmp.rows]
    return _table(("Document type", "Doc", "Field", "Expected", "Extracted", "Match"), rows)


def accuracy(cmp: Comparison) -> Dict[Tuple[str, str], Tuple[int, int]]:
    totals: Dict[Tuple[str, str], List[int]] = {}
    for r in cmp.rows:
        cell = totals.setdefault((r.docType, r.name), [0, 0])
        cell[0] += int(r.match)
        cell[1] += 1
    return {k: (v[0], v[1]) for k, v in sorted(totals.items())}


def format_accuracy(cmp: Comparison) -> str:
    acc = accuracy(cmp)
    rows = [(t, f, c, n, f"{100.0 * c / n:.1f}") for (t, f), (c, n) in acc.items()]
    correct, total = sum(c for c, _ in acc.values()), sum(n for _, n in acc.values())
    if total:
        rows.append(("ALL", "(all fields)", correct, total, f"{100.0 * correct / total:.1f}"))
    return _table(("Document type", "Field", "Correct", "Total", "Accuracy %"), rows)


def format_report(cmp: Comparison, title: str, problems: Sequence[str] = ()) -> str:
    parts = [f"=== {title} ===", "", format_rows(cmp), "", format_accuracy(cmp)]
    iou_rows = [r for r in cmp.rows if r.iou is not None]
    if iou_rows:
        parts += ["", "Bounding-box IoU vs manifest (informational): " +
                  ", ".join(f"{r.docId}.{r.name}={r.iou:.2f}" for r in iou_rows)]
    parts += ["", "Unexpected extracted fields (not in the expected data):"]
    parts += [f"  {t} {d}.{f} = {v!r}" for t, d, f, v in cmp.unexpected] or ["  none"]
    parts += ["", "Comparisons that could not be performed:"]
    parts += [f"  {c}" for c in list(cmp.cannot_compare) + list(problems)] or ["  none"]
    return "\n".join(parts)


# ----------------------------------------------------------------------------- running the real pipeline on fixtures

def extract_from_ocr_pages(docs: Dict[str, Dict[int, Any]]) -> Tuple[Dict[str, List[Any]], Dict[str, str]]:
    """Run the REAL classifier and field-extractor analyzers on recorded OCR pages (docId -> {page: OcrPage}).

    Returns (docId -> ExtractedField list, docId -> docType of its first page). No OCR engine is involved.
    """
    from .analyzers.field_extractor import FieldExtractorAnalyzer
    from .analyzers.page_classifier import PageClassifierAnalyzer
    from .classify import classification_store
    from .config import Settings
    from .context import AnalysisContext
    from .models import PageRecord, RenderedDocument, RenderedPage
    from .ocr_fixtures import seed_ocr
    from .registry import AnalyzerRegistry, run_analyzers

    extracted: Dict[str, List[Any]] = {}
    doc_types: Dict[str, str] = {}
    for doc_id, pages in sorted(docs.items()):
        rendered = []
        for number, page in sorted(pages.items()):
            record = PageRecord(docId=doc_id, page=number, renderPath=f"derived/{doc_id}/page-{number}.png",
                                renderDpi=float(page.renderDpi), rotationDeg=0.0, deskewDeg=0.0,
                                docType="UNKNOWN", docTypeConfidence=0.0)
            rendered.append(RenderedPage(record, Path(f"page-{number}.png"), page.widthPx, page.heightPx))
        ctx = AnalysisContext(RenderedDocument(doc_id, Path("original"), "0" * 64, rendered),
                              Settings(data_dir=Path(".")))
        seed_ocr(ctx, pages)
        registry = AnalyzerRegistry()
        registry.register(PageClassifierAnalyzer)
        registry.register(FieldExtractorAnalyzer)
        outcome = run_analyzers(ctx, registry, 60.0)
        extracted[doc_id] = list(outcome.fields)
        first = classification_store(ctx).get(min(pages))
        doc_types[doc_id] = first.docType if first else "?"
    return extracted, doc_types


def accuracy_from_manifest(manifest_path: Optional[Path], fixtures_directory: Optional[Path]) -> Tuple[Optional[Comparison], List[str]]:
    """(comparison or None, reasons the comparison could not be performed in full)."""
    from .ocr_fixtures import list_fixtures, load_ocr_fixture

    blockers: List[str] = []
    if manifest_path is None:
        return None, ["samples/manifest.json was not found (set SAMPLES_MANIFEST): no expected values, nothing compared"]
    manifest = load_manifest(manifest_path)
    blockers += manifest.problems
    for doc_id, doc in sorted(manifest.docs.items()):
        if not doc.fields:
            blockers.append(f"{doc_id}: the manifest gives no expected field values, nothing to compare")
    manifest.docs = {d: doc for d, doc in manifest.docs.items() if doc.fields}
    if not manifest.docs:
        return None, blockers + ["the manifest lists no usable documents with expected field values"]
    if fixtures_directory is None:
        return None, blockers + ["contracts/fixtures/ocr/ was not found (set OCR_FIXTURES_DIR): no recorded OCR to extract from"]
    docs: Dict[str, Dict[int, Any]] = {}
    for doc_id, page, path in list_fixtures(fixtures_directory):
        if doc_id in manifest.docs:
            docs.setdefault(doc_id, {})[page] = load_ocr_fixture(path)
    extracted, doc_types = extract_from_ocr_pages(docs)
    return compare(manifest, extracted, doc_types), blockers
