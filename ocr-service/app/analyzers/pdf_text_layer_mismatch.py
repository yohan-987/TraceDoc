"""Text layer vs render (O6): ``pdf.textlayer`` emits TEXT_LAYER_MISMATCH signals.

Compares the words a digital PDF carries in its text layer with what OCR reads on the rendered page. Words in the layer
that nothing visible shows (white-on-white, invisible render mode, covered text) and words whose visible reading differs
from the layer (stale or overlaid text) become signals. See app/textlayer_check.py for the method and README for limits.

Applies to digital PDFs only. Images and scans (no text layer) are NOT_APPLICABLE: with no layer there is nothing to compare,
which is not the same as "checked and clean".
"""
from __future__ import annotations

import logging
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pymupdf
from PIL import Image

from ..context import AnalysisContext
from ..models import AnalyzerResult, ForensicSignal, Provenance, Region
from ..ocr import get_engine, get_page_ocr
from ..ocr_core import OcrPageError, OcrUnavailableError, ocr_image_file
from ..registry import Analyzer, register_analyzer
from ..textlayer import get_text_layer
from ..textlayer_check import Mismatch, PageComparison, TooManyWordsError, compare_page, differing_chars, make_ink_fn
from ..textlayer_check_config import TextLayerCheckConfig, TextLayerCheckConfigError, load_text_layer_check_config

logger = logging.getLogger(__name__)
SIGNAL_TYPE = "TEXT_LAYER_MISMATCH"
_TEXT_LIMIT = 120


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str) -> str:
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT - 1] + "\u2026"


def _make_reread(ctx: AnalysisContext, cfg: TextLayerCheckConfig, page) -> Optional[Callable[[Tuple[float, float, float, float]], Optional[str]]]:
    """Second, sharper reading of a small area: re-render the PDF page ``rereadScale`` times larger and OCR the crop.

    Returns None when it cannot be done (switched off, no Tesseract, page too large to re-render): candidates are then
    reported as unverified rather than dropped."""
    if cfg.rereadScale <= 0:
        return None
    engine = get_engine(ctx)
    if not engine.available:
        return None
    scale = min(cfg.rereadScale, math.sqrt(ctx.settings.max_image_pixels / max(1, page.width_px * page.height_px)))
    if scale < 1.25:
        return None
    dpi = max(72, int(round(page.record.renderDpi * scale)))  # PyMuPDF only accepts an integer dpi

    def sharp_page() -> Image.Image:
        with pymupdf.open(str(ctx.document.source_path), filetype="pdf") as doc:
            pix = doc.load_page(page.page - 1).get_pixmap(dpi=dpi, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def reread(box) -> Optional[str]:
        try:
            image: Image.Image = ctx.get_or_compute(("tlmm", "sharp", page.page), sharp_page)
            width, height = image.size
            pad = cfg.rereadPadFrac * box[3]
            x0 = max(0, int((box[0] - pad * height / width) * width)); x1 = min(width, int(math.ceil((box[0] + box[2] + pad * height / width) * width)))
            y0 = max(0, int((box[1] - pad) * height)); y1 = min(height, int(math.ceil((box[1] + box[3] + pad) * height)))
            if x1 - x0 < 4 or y1 - y0 < 4:
                return None
            with tempfile.TemporaryDirectory(prefix="tlmm_") as tmp:
                path = Path(tmp) / "crop.png"
                image.crop((x0, y0, x1, y1)).save(path)
                raw = ocr_image_file(path, engine, dpi, cfg.rereadTimeoutS)
            return " ".join(w["text"] for w in raw["words"])
        except Exception as exc:  # noqa: BLE001 - a failed second reading just leaves the candidate unverified
            logger.warning("%s page %d: second reading failed (%s: %s)", ctx.doc_id, page.page, type(exc).__name__, exc)
            return None

    return reread


@register_analyzer
class PdfTextLayerMismatchAnalyzer(Analyzer):
    name = "pdf.textlayer"  # the id the engine config, the frontend and the recorded fixtures use
    version = "0.1"

    def _provenance(self, ctx: AnalysisContext, cfg: TextLayerCheckConfig, page: int, engine: str) -> Provenance:
        config = {**cfg.as_provenance(), "page": page, "ocrEngine": engine}
        return Provenance(analyzer=self.name, analyzerVersion=self.version, modelVersion=f"tlmm-{cfg.configVersion}",
                          config=config, inputSha256=ctx.input_sha256, processedAt=_now(), status="AVAILABLE")

    def _signals(self, ctx: AnalysisContext, cfg: TextLayerCheckConfig, page: int, ocr_engine: str,
                 cmp: PageComparison) -> List[ForensicSignal]:
        def region(box, label) -> Region:
            return Region(docId=ctx.doc_id, page=page, bbox=tuple(round(v, 6) for v in box), label=label)  # type: ignore[arg-type]

        out: List[ForensicSignal] = []
        prov = self._provenance(ctx, cfg, page, ocr_engine)
        base = {"pageAgreement": round(cmp.agreement, 3), "pageLayerWords": cmp.layer_words, "pageOcrWords": cmp.ocr_words}
        if cmp.page_level:
            out.append(ForensicSignal(
                signalId=f"{ctx.doc_id}-tlmm-p{page}-00", detector=self.name, type=SIGNAL_TYPE,
                regions=[region((0.0, 0.0, 1.0, 1.0), "whole page: the text layer and the page image barely agree")],
                confidence=cfg.pageLevelConfidence,
                measurements={**base, "scope": "page",
                              "note": "OCR may be the cause (rotated or unreadable page); word-level differences were not assessed, blank hidden words are reported separately"},
                provenance=prov))
        for n, m in enumerate(cmp.mismatches, 1):
            m: Mismatch
            if m.kind == "layer_only":
                regions = [region(m.layer_box, "text layer words with nothing visible")]
                conf = min(cfg.hiddenConfidenceMax, cfg.hiddenConfidence + cfg.hiddenConfidencePerWord * (m.layer_words - 1))
            elif m.kind == "differs":
                regions = [region(m.layer_box, "what the text layer says")] if m.layer_box else []
                regions.append(region(m.render_box, "what the page shows"))
                conf = cfg.differsConfidence
            else:
                regions = [region(m.render_box, "visible text missing from the text layer")]
                conf = cfg.renderOnlyConfidence
            # names follow the recorded fixture (contracts/fixtures/analyze/D-03.json); the rest explain how it was found
            layer_text = m.layer_text if m.kind != "render_only" else ""
            rendered_text = m.render_text if m.kind != "layer_only" else ""
            measurements: Dict[str, object] = {**base, "scope": "words", "kind": m.kind, "layerWords": m.layer_words,
                                               "ocrWordsHere": m.render_words,
                                               "renderedText": _clip(rendered_text), "textLayerText": _clip(layer_text),
                                               "differingChars": differing_chars(layer_text, rendered_text)}
            if m.kind == "layer_only":
                measurements["reason"] = m.reason
            if m.reread_text is not None:
                measurements["rereadText"] = _clip(m.reread_text)  # a sharper second reading that still disagrees with the layer
            out.append(ForensicSignal(signalId=f"{ctx.doc_id}-tlmm-p{page}-{n:02d}", detector=self.name, type=SIGNAL_TYPE,
                                      regions=regions, confidence=round(conf, 3), measurements=measurements,  # type: ignore[arg-type]
                                      provenance=prov))
        return out

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        try:
            cfg = load_text_layer_check_config()
        except TextLayerCheckConfigError as exc:
            return AnalyzerResult(status="ERROR", reason=str(exc))
        layer = get_text_layer(ctx)
        if layer.not_pdf:
            return AnalyzerResult.not_applicable("original is not a PDF, so there is no text layer to compare")
        if layer.error:
            return AnalyzerResult(status="ERROR", reason=layer.error)
        signals: List[ForensicSignal] = []
        compared: List[int] = []
        not_comparable: List[int] = []
        no_layer: List[int] = []
        not_comparable: List[int] = []
        failed: Dict[int, str] = {}
        unread = ignored = dropped = reread_dropped = unverified = 0
        engine = ""
        for page in ctx.pages:
            if page.page in layer.errors:
                failed[page.page] = f"text-layer extraction failed: {layer.errors[page.page]}"[:120]
                continue
            tl = layer.pages.get(page.page)
            if tl is None or not tl.page.words:
                no_layer.append(page.page)
                continue
            try:
                ocr = get_page_ocr(ctx, page.page)
            except OcrUnavailableError as exc:
                return AnalyzerResult.unavailable(f"the rendered page cannot be read, so it cannot be compared: {exc}")
            except OcrPageError as exc:
                failed[page.page] = f"OCR failed: {exc}"[:120]
                continue
            try:
                with Image.open(page.path) as img:
                    ink = make_ink_fn(img, cfg)
                cmp = compare_page(tl.page, ocr, ink, cfg, _make_reread(ctx, cfg, page))
            except TooManyWordsError as exc:
                failed[page.page] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one unreadable page must not hide the others
                failed[page.page] = f"{type(exc).__name__}: {exc}"[:120]
                continue
            engine = ocr.engine
            if cmp.not_comparable:
                not_comparable.append(page.page)
            else:
                compared.append(page.page)
            unread += cmp.unread_ink
            ignored += cmp.ignored_layer_words
            dropped += cmp.dropped
            reread_dropped += cmp.reread_dropped
            unverified += cmp.unverified
            signals.extend(self._signals(ctx, cfg, page.page, ocr.engine, cmp))
        config: Dict[str, object] = {**cfg.as_provenance(), "pages": len(ctx.pages), "pagesCompared": len(compared),
                                     "signals": len(signals), "layerWordsWithInkButNoOcr": unread,
                                     "layerWordsIgnored": ignored, "signalsDropped": dropped,
                                     "differsExplainedByRereading": reread_dropped, "differsUnverified": unverified}
        if not_comparable:
            config["pagesNotComparable"] = ",".join(str(n) for n in not_comparable)
        if no_layer:
            config["pagesWithoutTextLayer"] = ",".join(str(n) for n in no_layer)
        model_version = f"tlmm-{cfg.configVersion}"
        if failed:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(failed))
            return AnalyzerResult(status="ERROR", reason="; ".join(f"page {n}: {m}" for n, m in sorted(failed.items())),
                                  model_version=model_version, config=config)  # type: ignore[arg-type]
        if not compared and not_comparable:
            pages = ", ".join(str(n) for n in not_comparable)
            if signals:  # blank hidden words were still found
                return AnalyzerResult(status="AVAILABLE", reason=f"OCR could not read page(s) {pages}: only blank hidden words were checked there",
                                      signals=signals, model_version=model_version, config=config)  # type: ignore[arg-type]
            return AnalyzerResult.unavailable(f"OCR read too little of page(s) {pages} to compare it with the text layer")
        if not compared:
            return AnalyzerResult.not_applicable("the PDF has no text layer (scanned or image-only), so there is nothing to compare")
        reason = (f"page(s) {', '.join(str(n) for n in not_comparable)} could not be compared: OCR read too little of them"
                  if not_comparable else None)
        return AnalyzerResult(status="AVAILABLE", reason=reason, signals=signals, model_version=model_version,
                              config=config)  # type: ignore[arg-type]

