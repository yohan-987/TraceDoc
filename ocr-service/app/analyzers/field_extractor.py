"""Field extractor plugin (registered as "page.field_extractor").

For each page whose document type is id_card, application_form or bank_statement it extracts the schema's fields
(app/schemas/*.yaml) from the OCR already in the shared context and returns contract ExtractedField objects.
The type comes from the O3 classifier (classification_store); if the classifier did not run, the same pure classify_page is
applied to the page's OCR. UNKNOWN, AMBIGUOUS and "other" pages are skipped, never forced into a type.
OCR is read from the context (cached live OCR or seeded fixtures) and only computed, once, if nobody has done it.
Fields are only reported when found: a missing field is listed in provenance, never fabricated, and low-confidence or
format-invalid fields are returned with their flags, never dropped.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from ..classifier_config import ClassifierConfig, ClassifierConfigError, load_classifier_config
from ..classify import classification_store, classify_page
from ..context import AnalysisContext
from ..extract import FieldResult, extract_fields
from ..extract_schema import SUPPORTED_DOC_TYPES, DocSchema, SchemaError, load_schema, load_settings
from ..models import AnalyzerResult, ExtractedField, OcrPage, Region
from ..ocr import OcrPageError, OcrUnavailableError, cached_page_ocr, get_page_ocr
from ..ocr_core import page_timeout_s
from ..registry import Analyzer, register_analyzer

_BUDGET_FRACTION = 0.9
_STORE_KEY = ("field-extraction",)


def extraction_store(ctx: AnalysisContext) -> Dict[str, FieldResult]:
    """Per-request {fieldId: FieldResult} (includes semanticType), for analyzers that run later."""
    return ctx.get_or_compute(_STORE_KEY, dict)


def _short(items: List[str], limit: int = 300) -> str:
    return ",".join(items)[:limit] or "none"


@register_analyzer
class FieldExtractorAnalyzer(Analyzer):
    name = "page.field_extractor"   # sorts after "page.classifier", so the classification is normally ready
    version = "0.1"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        try:
            settings = load_settings()
        except SchemaError as exc:
            return AnalyzerResult(status="ERROR", reason=str(exc))

        classes = classification_store(ctx)
        deadline = time.monotonic() + ctx.settings.analyzer_timeout_s * _BUDGET_FRACTION
        schemas: Dict[str, DocSchema] = {}
        problems: Dict[int, str] = {}
        unavailable: Dict[int, str] = {}
        fields: List[ExtractedField] = []
        results: Dict[str, FieldResult] = {}
        page_config: Dict[str, object] = {}
        used_types: Dict[str, str] = {}
        pages_extracted = missing_total = 0
        cls_cfg: Optional[ClassifierConfig] = None
        cls_cfg_loaded = False

        for page in ctx.pages:
            prefix = f"page{page.page}"
            ocr_page: Optional[OcrPage] = cached_page_ocr(ctx, page.page)
            if ocr_page is None:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0.5:
                        raise OcrPageError("analyzer time budget exhausted")
                    ocr_page = get_page_ocr(ctx, page.page, timeout_s=min(page_timeout_s(), remaining))
                except OcrUnavailableError as exc:
                    unavailable[page.page] = str(exc)
                    continue
                except OcrPageError as exc:
                    problems[page.page] = str(exc)
                    continue

            classified = classes.get(page.page)
            if classified is not None:
                doc_type, source = classified.docType, "classifier"
            else:
                if not cls_cfg_loaded:
                    cls_cfg_loaded = True
                    try:
                        cls_cfg = load_classifier_config()
                    except ClassifierConfigError:
                        cls_cfg = None
                doc_type = classify_page(ocr_page, cls_cfg).docType if cls_cfg else "UNKNOWN"
                source = "inline" if cls_cfg else "unclassified"
            page_config[f"{prefix}.docType"] = doc_type
            page_config[f"{prefix}.docTypeSource"] = source
            if doc_type not in SUPPORTED_DOC_TYPES:
                page_config[f"{prefix}.skipped"] = f"no extraction schema for docType {doc_type}"
                continue

            try:
                if doc_type not in schemas:
                    schemas[doc_type] = load_schema(doc_type)
                    used_types[doc_type] = schemas[doc_type].schemaVersion
                outcome = extract_fields(ocr_page, schemas[doc_type], settings)
            except SchemaError as exc:
                problems[page.page] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one page must not take the analyzer down
                problems[page.page] = f"extraction failed ({type(exc).__name__})"
                continue

            pages_extracted += 1
            low, invalid = [], []
            for r in outcome.results:
                field_id = f"FLD-{len(fields) + 1:03d}"
                fields.append(ExtractedField(
                    fieldId=field_id, docId=ctx.doc_id, name=r.name, rawValue=r.rawValue,
                    normalizedValue=r.normalizedValue, ocrConfidence=r.ocrConfidence,
                    extractionConfidence=r.extractionConfidence, validationStatus=r.validationStatus,
                    region=Region(docId=ctx.doc_id, page=page.page, bbox=tuple(round(v, 6) for v in r.bbox), label=r.name),
                    source="ocr"))
                results[field_id] = r
                if r.strategy != "anchor_region" and r.ocrConfidence < settings.lowOcrConfidence:
                    low.append(f"{r.name}(ocr={r.ocrConfidence:.2f})")
                if r.extractionConfidence < settings.lowExtractionConfidence:
                    low.append(f"{r.name}(extraction={r.extractionConfidence:.2f})")
                if r.validationStatus == "INVALID_FORMAT":
                    invalid.append(r.name)
            missing_total += len(outcome.missing)
            page_config[f"{prefix}.fields"] = _short([f"{r.name}:{r.strategy}" for r in outcome.results])
            page_config[f"{prefix}.missing"] = _short(outcome.missing)
            if low:
                page_config[f"{prefix}.lowConfidence"] = _short(low)
            if invalid:
                page_config[f"{prefix}.invalidFormat"] = _short(invalid)

        extraction_store(ctx).update(results)

        config: Dict[str, object] = {
            "settingsVersion": settings.settingsVersion, "lowOcrConfidence": settings.lowOcrConfidence,
            "lowExtractionConfidence": settings.lowExtractionConfidence,
            "dateOrder": settings.dateOrder or "none", "pages": len(ctx.pages), "pagesExtracted": pages_extracted,
            "fields": len(fields), "fieldsMissing": missing_total,
        }
        config.update({f"scoring.{k}": v for k, v in settings.scoring.items()})
        config.update({f"schema.{t}": v for t, v in sorted(used_types.items())})
        config.update(page_config)

        if unavailable and not problems and len(unavailable) == len(ctx.pages):
            return AnalyzerResult.unavailable(f"OCR is not available: {next(iter(unavailable.values()))}")
        failed = {**unavailable, **problems}
        if failed:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(failed))
            reason = "; ".join(f"page {n}: {msg}" for n, msg in sorted(failed.items()))
            return AnalyzerResult(status="ERROR", reason=reason, model_version=f"schemas-{settings.settingsVersion}",
                                  config=config)
        if pages_extracted == 0:
            return AnalyzerResult(status="NOT_APPLICABLE",
                                  reason="no page is classified as id_card, application_form or bank_statement",
                                  config=config)
        return AnalyzerResult(status="AVAILABLE", fields=fields, model_version=f"schemas-{settings.settingsVersion}",
                              config=config)
