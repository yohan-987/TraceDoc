"""Real-D1 accuracy evaluation: run the actual pipeline on the D1 source files and compare with samples/manifest.json.

This is separate from the C1 report in app/extract_report.py: C1 recorded-OCR fixtures (D-01, D-02, D-03) are NOT D1
documents and are never compared against D1 expected values.

Manifest shape (the D1 manifest): {"files": [{"doc_id", "doc_type", "variant", "path", "page",
"fields": [{"name", "value", "bbox": [x, y, w, h] (normalized), optional "page"}]}]}. Digital and scan variants share a
doc_id, so every file gets its own runtime id ("<doc_id>__<variant>"). Field names may repeat (e.g. several txn_amount).

Result vocabulary per expected field: MATCH, MISMATCH (a value was extracted but differs), MISSING (supported field, nothing
extracted), NOT EVALUATED (the O4 schema has no such field: not an extraction failure), FAILED (the sample's render / OCR /
analyzers failed, so nothing was compared). Extracted fields nobody expected are UNEXPECTED and reported separately.
Bounding-box IoU is informational only and never part of value accuracy.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

BBox = Tuple[float, float, float, float]

MATCH, MISMATCH, MISSING = "MATCH", "MISMATCH", "MISSING"
NOT_EVALUATED, FAILED, UNEXPECTED = "NOT EVALUATED", "FAILED", "UNEXPECTED"
STATUS_OK, STATUS_ERROR, STATUS_UNAVAILABLE = "OK", "ERROR", "UNAVAILABLE"


# ----------------------------------------------------------------------------- manifest

@dataclass(frozen=True)
class ExpectedField:
    name: str
    value: str
    bbox: Optional[BBox]
    page: int
    order: int                       # position in the manifest, kept so repeated names stay distinct
    bbox_warning: Optional[str] = None


@dataclass(frozen=True)
class D1Sample:
    runtime_id: str                  # unique per file: "<doc_id>__<variant>"
    doc_id: str
    doc_type: str
    variant: str
    rel_path: str
    abs_path: Optional[Path]         # None when the file cannot be located (reported as an ERROR sample)
    page: int
    fields: Tuple[ExpectedField, ...]
    problems: Tuple[str, ...] = ()


@dataclass
class D1Manifest:
    path: Path
    samples: List[D1Sample] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def _bbox(raw: Any) -> Tuple[Optional[BBox], Optional[str]]:
    if isinstance(raw, dict):
        raw = [raw.get(k) for k in ("x", "y", "w", "h")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw):
        return None, "bbox is not four numbers" if raw is not None else None
    x, y, w, h = (float(v) for v in raw)
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.0001 or y + h > 1.0001:
        return (x, y, w, h), "bbox is not a normalized [x, y, w, h] box (is it [x0, y0, x1, y1]?): IoU skipped"
    return (x, y, w, h), None


def _locate(manifest_path: Path, rel: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a manifest path against the manifest folder, then its parent; never outside those two roots."""
    base = manifest_path.resolve().parent
    roots = [base, base.parent]
    if Path(rel).is_absolute():
        return None, "absolute paths are not accepted"
    for root in roots:
        candidate = (root / rel).resolve()
        if root != candidate and root not in candidate.parents:
            return None, "path escapes the samples folder"
        if candidate.is_file():
            return candidate, None
    return None, f"file not found: {rel}"


def parse_d1_manifest(path: Path) -> D1Manifest:
    """Parse the D1 manifest. Nothing is written or modified; malformed entries are listed, never guessed."""
    manifest = D1Manifest(path=Path(path))
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        manifest.problems.append(f"{Path(path).name}: unreadable ({type(exc).__name__})")
        return manifest
    # entries live under "files" (older D1 manifests) or "documents" (current ones)
    files = next((data[k] for k in ("files", "documents") if isinstance(data, dict) and isinstance(data.get(k), list)), None)
    if files is None:
        manifest.problems.append(f"{Path(path).name}: no top-level 'files' or 'documents' list")
        return manifest
    used: Set[str] = set()
    for i, entry in enumerate(files):
        label = f"files[{i}]"
        if not isinstance(entry, dict):
            manifest.problems.append(f"{label}: not an object")
            continue
        rel = entry.get("path") if isinstance(entry.get("path"), str) and entry.get("path") else entry.get("file")  # "file" in current manifests
        entry = {**entry, "path": rel}
        missing = [k for k in ("doc_id", "doc_type", "path") if not isinstance(entry.get(k), str) or not entry.get(k)]
        if missing:
            manifest.problems.append(f"{label}: missing {missing}; entry skipped")
            continue
        doc_id, variant = entry["doc_id"], str(entry.get("variant") or "unknown")
        base = re.sub(r"[^A-Za-z0-9_-]", "-", f"{doc_id}__{variant}")
        runtime_id, n = base, 1
        while runtime_id in used:
            n += 1
            runtime_id = f"{base}__{n}"
        used.add(runtime_id)
        page = entry.get("page", 1)
        page = page if isinstance(page, int) and not isinstance(page, bool) and page >= 1 else 1
        problems: List[str] = []
        fields: List[ExpectedField] = []
        raw_fields = entry.get("fields")
        if not isinstance(raw_fields, list):
            problems.append("'fields' is not a list")
            raw_fields = []
        for j, f in enumerate(raw_fields):
            name = f.get("name") if isinstance(f, dict) else None
            value = f.get("value") if isinstance(f, dict) else None
            if not isinstance(name, str) or not name or value is None:
                problems.append(f"fields[{j}]: needs a name and a value; skipped")
                continue
            region = f.get("region") if isinstance(f, dict) else None
            # current manifests keep "bbox" as [x_min, y_min, x_max, y_max] and the contract-shaped [x, y, w, h] in "region"
            raw_box = region.get("bbox") if isinstance(region, dict) and region.get("bbox") is not None else f.get("bbox")
            box, warning = _bbox(raw_box)
            fpage = f.get("page", page)
            fpage = fpage if isinstance(fpage, int) and not isinstance(fpage, bool) and fpage >= 1 else page
            fields.append(ExpectedField(name, str(value), box, fpage, j, warning))
        abs_path, why = _locate(Path(path), entry["path"])
        if why:
            problems.append(why)
        manifest.samples.append(D1Sample(runtime_id, doc_id, entry["doc_type"], variant, entry["path"], abs_path, page,
                                         tuple(fields), tuple(problems)))
    return manifest


# ----------------------------------------------------------------------------- matching

@dataclass
class FieldOutcome:
    name: str
    page: int
    result: str
    expected: Optional[ExpectedField] = None
    extracted: Any = None
    match_on: str = "-"              # normalizedValue | rawValue | -
    iou: Optional[float] = None      # informational
    reason: Optional[str] = None


@dataclass
class SampleResult:
    sample: D1Sample
    status: str = STATUS_OK          # OK | ERROR | UNAVAILABLE
    reasons: List[str] = field(default_factory=list)
    classified_as: str = "?"
    engines: str = "-"
    outcomes: List[FieldOutcome] = field(default_factory=list)


def _iou(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _values_match(expected: str, extracted: Any) -> str:
    """'normalizedValue' / 'rawValue' when the expected value equals it exactly (surrounding spaces ignored), else '-'."""
    want = expected.strip()
    if extracted.normalizedValue is not None and extracted.normalizedValue.strip() == want:
        return "normalizedValue"
    if extracted.rawValue.strip() == want:
        return "rawValue"
    return "-"


def _pair_iou(exp: ExpectedField, ext: Any) -> Optional[float]:
    if exp.bbox is None or exp.bbox_warning:
        return None
    return round(_iou(exp.bbox, tuple(ext.region.bbox)), 4)


def _reading_key(box: Optional[Sequence[float]], fallback: int) -> Tuple[float, float, int]:
    return (round(box[1], 3), box[0], fallback) if box else (float("inf"), float("inf"), fallback)


def match_group(expected: Sequence[ExpectedField], extracted: Sequence[Any]) -> Tuple[List[FieldOutcome], List[Any]]:
    """One-to-one matching of the expected and extracted fields that share a name and a page.

    1. Exact value matches are paired first, each extracted field used at most once (several identical expected values
       need several extracted ones).
    2. Left-overs are paired in reading order and reported as MISMATCH (a value was found but differs).
    3. Remaining expected fields are MISSING; remaining extracted fields are returned as unexpected.
    """
    name, page = expected[0].name, expected[0].page
    exp = sorted(expected, key=lambda e: _reading_key(e.bbox, e.order))
    ext = sorted(extracted, key=lambda x: _reading_key(tuple(x.region.bbox), 0))
    free = list(range(len(ext)))
    pairs: Dict[int, Tuple[int, str]] = {}
    for ei, e in enumerate(exp):
        options = [(xi, _values_match(e.value, ext[xi])) for xi in free]
        options = [(xi, how) for xi, how in options if how != "-"]
        if options:
            best = max(options, key=lambda o: (_pair_iou(e, ext[o[0]]) or 0.0, -o[0]))
            pairs[ei] = best
            free.remove(best[0])
    outcomes: List[FieldOutcome] = []
    leftovers_e = [ei for ei in range(len(exp)) if ei not in pairs]
    for ei in leftovers_e:
        if free:
            xi = free.pop(0)
            pairs[ei] = (xi, "-")
    for ei, e in enumerate(exp):
        if ei in pairs:
            xi, how = pairs[ei]
            x = ext[xi]
            outcomes.append(FieldOutcome(name, page, MATCH if how != "-" else MISMATCH, e, x, how, _pair_iou(e, x)))
        else:
            outcomes.append(FieldOutcome(name, page, MISSING, e, None, reason="no extracted value for this field"))
    used = {pairs[ei][0] for ei in pairs}
    return outcomes, [ext[i] for i in range(len(ext)) if i not in used]


def compare_sample(sample: D1Sample, supported: Optional[Set[str]], run_status: str, extracted: Sequence[Any],
                   reasons: Sequence[str] = ()) -> SampleResult:
    """Compare one sample. ``supported``: field names of the O4 schema for its doc_type (None: no schema at all)."""
    result = SampleResult(sample, run_status, list(reasons))
    failed = run_status != STATUS_OK
    groups: Dict[Tuple[str, int], List[ExpectedField]] = {}
    for e in sample.fields:
        if supported is None or e.name not in supported:
            why = (f"no O4 schema for doc_type '{sample.doc_type}'" if supported is None
                   else f"the O4 '{sample.doc_type}' schema has no field '{e.name}'")
            result.outcomes.append(FieldOutcome(e.name, e.page, NOT_EVALUATED, e, reason=why))
        elif failed:
            result.outcomes.append(FieldOutcome(e.name, e.page, FAILED, e,
                                                reason=f"sample {run_status}: " + "; ".join(reasons)[:200]))
        else:
            groups.setdefault((e.name, e.page), []).append(e)
    if failed:
        return result
    pool: Dict[Tuple[str, int], List[Any]] = {}
    for x in extracted:
        pool.setdefault((x.name, x.region.page), []).append(x)
    for key, exp in groups.items():
        outcomes, left = match_group(exp, pool.pop(key, []))
        result.outcomes += outcomes
        pool[key] = left                       # extracted fields of this name/page that nobody expected
    for (name, page), leftover in pool.items():
        for x in leftover:
            result.outcomes.append(FieldOutcome(name, page, UNEXPECTED, None, x))
    result.outcomes.sort(key=lambda o: (o.page, o.expected.order if o.expected else 10**6, o.name))
    return result


# ----------------------------------------------------------------------------- aggregation

@dataclass
class Tally:
    samples: int = 0
    samples_ok: int = 0
    expected: int = 0
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    not_evaluated: int = 0
    failed: int = 0
    unexpected: int = 0
    ious: List[float] = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return self.matched + self.mismatched + self.missing

    @property
    def accuracy_evaluated(self) -> Optional[float]:
        return self.matched / self.evaluated if self.evaluated else None

    @property
    def accuracy_strict(self) -> Optional[float]:
        """matched / every expected field: unsupported and failed fields count as not matched."""
        return self.matched / self.expected if self.expected else None

    @property
    def mean_iou(self) -> Optional[float]:
        return sum(self.ious) / len(self.ious) if self.ious else None


def tally(results: Iterable[SampleResult]) -> Tally:
    t = Tally()
    for r in results:
        t.samples += 1
        t.samples_ok += int(r.status == STATUS_OK)
        for o in r.outcomes:
            if o.result == UNEXPECTED:
                t.unexpected += 1
                continue
            t.expected += 1
            if o.result == MATCH:
                t.matched += 1
            elif o.result == MISMATCH:
                t.mismatched += 1
            elif o.result == MISSING:
                t.missing += 1
            elif o.result == NOT_EVALUATED:
                t.not_evaluated += 1
            elif o.result == FAILED:
                t.failed += 1
            if o.iou is not None:
                t.ious.append(o.iou)
    return t


def by_variant(results: Sequence[SampleResult]) -> Dict[str, Tally]:
    variants = sorted({r.sample.variant for r in results})
    return {v: tally(r for r in results if r.sample.variant == v) for v in variants}


# ----------------------------------------------------------------------------- report

def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    fmt = lambda cells: " | ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([fmt(headers), "-+-".join("-" * w for w in widths)] + [fmt(r) for r in rows])


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{100.0 * v:.1f}"


def _shown(x: Any) -> str:
    if x is None:
        return "<none>"
    norm = x.normalizedValue if x.normalizedValue is not None else "<not normalized>"
    return norm if norm == x.rawValue else f"{norm}  (raw: {x.rawValue!r})"


def format_sample(r: SampleResult) -> str:
    s = r.sample
    head = (f"--- {s.runtime_id}  [manifest doc_id={s.doc_id} variant={s.variant} doc_type={s.doc_type} page={s.page}]\n"
            f"    file: {s.rel_path}\n    status: {r.status}  classified as: {r.classified_as}  OCR engine: {r.engines}")
    if r.reasons:
        head += "\n    reasons: " + "; ".join(r.reasons)
    for p in s.problems:
        head += f"\n    manifest note: {p}"
    rows = [(o.page, o.name, o.expected.value if o.expected else "<not expected>", _shown(o.extracted), o.result,
             "-" if o.iou is None else f"{o.iou:.2f}", o.reason or "") for o in r.outcomes]
    body = _table(("Page", "Field", "Expected", "Extracted", "Result", "IoU", "Note"), rows) if rows else "    (no expected fields)"
    return head + "\n" + body


def _tally_row(label: str, t: Tally) -> Tuple[str, ...]:
    return (label, f"{t.samples_ok}/{t.samples}", t.expected, t.evaluated, t.matched, t.mismatched, t.missing,
            t.not_evaluated, t.failed, t.unexpected, _pct(t.accuracy_evaluated), _pct(t.accuracy_strict),
            "-" if t.mean_iou is None else f"{t.mean_iou:.2f}")


def format_report(manifest: D1Manifest, results: Sequence[SampleResult], title: str = "D1 ACCURACY (real pipeline run)") -> str:
    parts = [f"=== {title} ===", f"manifest: {manifest.path}", ""]
    parts += [format_sample(r) + "\n" for r in results]
    rows = [_tally_row(v, t) for v, t in by_variant(results).items()] + [_tally_row("OVERALL", tally(results))]
    parts += ["=== SUMMARY ===", _table(
        ("Group", "Samples ok", "Expected", "Evaluated", "Matched", "Mismatched", "Missing", "Not evaluated", "Failed",
         "Unexpected", "Acc % (of evaluated)", "Acc % (of all expected)", "Mean IoU*"), rows)]
    total = tally(results)
    parts += ["", "Evaluated = matched + mismatched + missing. 'Not evaluated' fields (no O4 schema field) and 'Failed' fields "
                  "(sample could not be processed) are NOT extraction mistakes and are NOT in 'Acc % (of evaluated)'; they ARE in "
                  "'Acc % (of all expected)'. * IoU is informational bbox overlap and is not part of value accuracy."]
    if total.not_evaluated or total.failed or total.evaluated < total.expected:
        parts.append(f"COVERAGE IS INCOMPLETE: only {total.evaluated} of {total.expected} expected fields were evaluated "
                     f"({total.not_evaluated} not evaluated, {total.failed} failed); no overall accuracy claim should be made "
                     "from the 'of evaluated' column alone.")
    if manifest.problems:
        parts += ["", "Manifest problems:"] + [f"  - {p}" for p in manifest.problems]
    return "\n".join(parts)


def to_jsonable(manifest: D1Manifest, results: Sequence[SampleResult]) -> Dict[str, Any]:
    def tally_json(t: Tally) -> Dict[str, Any]:
        return {"samples": t.samples, "samplesOk": t.samples_ok, "expected": t.expected, "evaluated": t.evaluated,
                "matched": t.matched, "mismatched": t.mismatched, "missing": t.missing, "notEvaluated": t.not_evaluated,
                "failed": t.failed, "unexpected": t.unexpected, "accuracyOfEvaluated": t.accuracy_evaluated,
                "accuracyOfAllExpected": t.accuracy_strict, "meanIoU": t.mean_iou}
    return {
        "manifest": str(manifest.path), "manifestProblems": manifest.problems,
        "samples": [{"runtimeId": r.sample.runtime_id, "docId": r.sample.doc_id, "variant": r.sample.variant,
                     "docType": r.sample.doc_type, "path": r.sample.rel_path, "status": r.status, "reasons": r.reasons,
                     "classifiedAs": r.classified_as, "engines": r.engines,
                     "fields": [{"name": o.name, "page": o.page, "result": o.result,
                                 "expected": o.expected.value if o.expected else None,
                                 "extractedNormalized": o.extracted.normalizedValue if o.extracted else None,
                                 "extractedRaw": o.extracted.rawValue if o.extracted else None,
                                 "matchedOn": o.match_on, "iou": o.iou, "note": o.reason} for o in r.outcomes]}
                    for r in results],
        "byVariant": {v: tally_json(t) for v, t in by_variant(results).items()}, "overall": tally_json(tally(results)),
    }


# ----------------------------------------------------------------------------- running the real pipeline

@dataclass
class PipelineRun:
    status: str = STATUS_OK
    reasons: List[str] = field(default_factory=list)
    fields: List[Any] = field(default_factory=list)
    classified_as: str = "?"
    engines: str = "-"
    statuses: Dict[str, str] = field(default_factory=dict)


_CRITICAL = ("ocr", "page.classifier", "page.field_extractor")


def supported_fields(doc_type: str) -> Optional[Set[str]]:
    """Field names of the O4 schema for ``doc_type`` (None when there is no schema or it cannot be loaded)."""
    from .extract_schema import SchemaError, load_schema
    try:
        return {f.name for f in load_schema(doc_type).fields}
    except SchemaError:
        return None


def run_sample_pipeline(sample: D1Sample, workdir: Path, dpi: int = 150, timeout: float = 120.0,
                        registry: Any = None) -> PipelineRun:
    """Render, OCR, classify and extract one sample with the production code (the same steps as POST /analyze).

    The source file is only copied (never modified) to <workdir>/evidence/<runtime_id>/original.<ext>.
    """
    from .classify import classification_store
    from .config import Settings
    from .context import AnalysisContext
    from .registry import default_registry, discover_analyzers, run_analyzers
    from .render import RenderError, render_document

    if sample.abs_path is None:
        return PipelineRun(STATUS_ERROR, [next((p for p in sample.problems if "file" in p or "path" in p), "source file missing")])
    settings = Settings(data_dir=Path(workdir), render_dpi=dpi, analyzer_timeout_s=timeout)
    folder = settings.evidence_dir / sample.runtime_id
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample.abs_path, folder / ("original" + sample.abs_path.suffix.lower()))
    try:
        document = render_document(sample.runtime_id, settings)
    except RenderError as exc:
        return PipelineRun(STATUS_ERROR, [f"render failed: {exc}"])
    except Exception as exc:  # noqa: BLE001 - a broken sample must not stop the other five
        return PipelineRun(STATUS_ERROR, [f"render failed: {type(exc).__name__}"])
    if registry is None:
        discover_analyzers()
        registry = default_registry
    ctx = AnalysisContext(document, settings)
    try:
        outcome = run_analyzers(ctx, registry, timeout)
    except Exception as exc:  # noqa: BLE001
        return PipelineRun(STATUS_ERROR, [f"analysis failed: {type(exc).__name__}"])
    run = PipelineRun(fields=list(outcome.fields), statuses=dict(outcome.statuses))
    prov = {p.analyzer: p for p in outcome.provenance}
    bad = {STATUS_ERROR: [], STATUS_UNAVAILABLE: []}
    for name in _CRITICAL:
        status = outcome.statuses.get(name)
        if status is None:
            bad[STATUS_ERROR].append(f"{name}: not registered")
        elif status in bad:
            reason = prov[name].config.get("reason", "no reason given") if name in prov else "no reason given"
            bad[status].append(f"{name}: {reason}")
    if bad[STATUS_ERROR]:
        run.status, run.reasons = STATUS_ERROR, bad[STATUS_ERROR] + bad[STATUS_UNAVAILABLE]
    elif bad[STATUS_UNAVAILABLE]:
        run.status, run.reasons = STATUS_UNAVAILABLE, bad[STATUS_UNAVAILABLE]
    classified = classification_store(ctx).get(sample.page)
    run.classified_as = classified.docType if classified else "?"
    if "ocr" in prov:
        run.engines = str(prov["ocr"].config.get("engines", "-"))
    return run


def evaluate(manifest: D1Manifest, workdir: Path, dpi: int = 150, timeout: float = 120.0, registry: Any = None,
             runner: Any = None) -> List[SampleResult]:
    """Run every manifest sample independently and compare it with its own expected fields."""
    runner = runner or run_sample_pipeline
    results: List[SampleResult] = []
    for sample in manifest.samples:
        try:
            run = runner(sample, workdir, dpi, timeout, registry)
        except Exception as exc:  # noqa: BLE001 - one broken sample never hides the others
            run = PipelineRun(STATUS_ERROR, [f"evaluator error: {type(exc).__name__}"])
        result = compare_sample(sample, supported_fields(sample.doc_type), run.status, run.fields, run.reasons)
        result.classified_as, result.engines = run.classified_as, run.engines
        results.append(result)
    return results
