"""Image forensics (O8): ``image.ela``, ``image.noise`` and ``image.copymove``.

Three analyzers share one page loader. Signals are observations about pixels, never verdicts:
  * image.ela       ELA_ANOMALY          - JPEG-originals only; low confidence (see README, measured limits);
  * image.noise     NOISE_INCONSISTENCY  - paper-only blocks far quieter than the rest of the page;
  * image.copymove  COPY_MOVE            - two regions holding the same ink pixels (one signal, two regions).

Applies to image originals and to scanned PDF pages (no text-layer words). A digital PDF page is NOT_APPLICABLE.
A test that cannot be run (a clean digital image has no noise to measure) is reported as NOT_APPLICABLE with the
reason; it is never reported as a clean result.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from PIL import Image

from .. import image_forensics as algo
from ..context import AnalysisContext
from ..image_forensics_config import ImageForensicsConfig, ImageForensicsConfigError, load_image_forensics_config
from ..models import AnalyzerResult, ForensicSignal, Provenance, Region
from ..ocr_core import normalize_box
from ..registry import Analyzer, register_analyzer
from ..render import looks_like_pdf
from ..textlayer import get_text_layer

_ELA_MAX_CONFIDENCE = 0.4  # on the D2 samples ELA found nothing; it only ever supports other evidence


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _PageMode:
    kind: Optional[str]        # "image", "scanned_pdf" or None (not applicable)
    reason: str = ""
    jpeg_original: bool = False


def _page_modes(ctx: AnalysisContext) -> Dict[int, _PageMode]:
    def compute() -> Dict[int, _PageMode]:
        src = ctx.document.source_path
        modes: Dict[int, _PageMode] = {}
        if looks_like_pdf(src):
            layer = get_text_layer(ctx)
            for page in ctx.pages:
                tl = layer.pages.get(page.page)
                if layer.error or tl is None:
                    modes[page.page] = _PageMode(None, "PDF text layer could not be read, so scanned pages cannot be told apart")
                elif tl.page.words:
                    modes[page.page] = _PageMode(None, "digital PDF page (has a text layer)")
                else:
                    modes[page.page] = _PageMode("scanned_pdf")
            return modes
        try:
            with Image.open(src) as im:
                is_jpeg, size = (im.format == "JPEG"), im.size
        except Exception:  # noqa: BLE001
            is_jpeg, size = False, (0, 0)
        for page in ctx.pages:
            same_grid = (page.width_px, page.height_px) == tuple(size) and page.record.rotationDeg == 0
            modes[page.page] = _PageMode("image", jpeg_original=bool(is_jpeg and same_grid))
        return modes

    return ctx.get_or_compute(("imgf", "modes"), compute)


def _working_image(ctx: AnalysisContext, page, cfg: ImageForensicsConfig) -> Image.Image:
    def load() -> Image.Image:
        with Image.open(page.path) as im:
            return algo.to_working(im.convert("RGB"), cfg)

    return ctx.get_or_compute(("imgf", "img", page.page), load)


def _provenance(analyzer: str, version: str, ctx: AnalysisContext, cfg: ImageForensicsConfig,
                extra: Dict[str, object]) -> Provenance:
    config = {**cfg.as_provenance(), **extra}
    return Provenance(analyzer=analyzer, analyzerVersion=version, modelVersion=f"imgf-{cfg.configVersion}",
                      config=config, inputSha256=ctx.input_sha256, processedAt=_now(), status="AVAILABLE")


def _region(ctx: AnalysisContext, page: int, box: algo.PixelBox, size, label: str) -> Optional[Region]:
    nb = normalize_box(box[0], box[1], box[2], box[3], size[0], size[1])
    return None if nb is None else Region(docId=ctx.doc_id, page=page, bbox=nb, label=label)


class _ImageStageAnalyzer(Analyzer):
    """Shared page loop; subclasses provide the stage and how a stage result becomes signals."""

    version = "0.1"
    stage_label = ""

    def _eligible(self, mode: _PageMode) -> Optional[str]:
        """None when the page can be tested, otherwise the reason it cannot."""
        return None if mode.kind else mode.reason

    def _stage(self, img: Image.Image, cfg: ImageForensicsConfig) -> algo.StageResult:  # pragma: no cover
        raise NotImplementedError

    def _signals(self, ctx, page, size, cfg, stage: algo.StageResult) -> List[ForensicSignal]:  # pragma: no cover
        raise NotImplementedError

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        try:
            cfg = load_image_forensics_config()
        except ImageForensicsConfigError as exc:
            return AnalyzerResult(status="ERROR", reason=str(exc))
        modes = _page_modes(ctx)
        signals: List[ForensicSignal] = []
        skipped: Dict[int, str] = {}
        failed: Dict[int, str] = {}
        tested = 0
        for page in ctx.pages:
            why = self._eligible(modes[page.page])
            if why:
                skipped[page.page] = why
                continue
            try:
                img = _working_image(ctx, page, cfg)
                result = ctx.get_or_compute(("imgf", self.name, page.page), lambda i=img: self._stage(i, cfg))
            except Exception as exc:  # noqa: BLE001 - one unreadable page must not hide the others
                failed[page.page] = f"{type(exc).__name__}: {exc}"[:120]
                continue
            if result.skipped:
                skipped[page.page] = result.skipped
                continue
            tested += 1
            signals.extend(self._signals(ctx, page, img.size, cfg, result))
        config: Dict[str, object] = {**cfg.as_provenance(), "pages": len(ctx.pages), "pagesTested": tested,
                                     "signals": len(signals)}
        if skipped:
            config["pagesSkipped"] = ",".join(str(n) for n in sorted(skipped))
        if failed:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(failed))
            return AnalyzerResult(status="ERROR", reason="; ".join(f"page {n}: {m}" for n, m in sorted(failed.items())),
                                  model_version=f"imgf-{cfg.configVersion}", config=config)
        if tested == 0:
            reason = next(iter(skipped.values()), "no page could be tested")
            return AnalyzerResult.not_applicable(f"{self.stage_label} not run: {reason}")
        for n, why in sorted(skipped.items()):
            config[f"skipped.page{n}"] = why[:120]
        return AnalyzerResult(status="AVAILABLE", signals=signals, model_version=f"imgf-{cfg.configVersion}",
                              config=config)


@register_analyzer
class ImageElaAnalyzer(_ImageStageAnalyzer):
    name = "image.ela"
    stage_label = "error-level analysis"

    def _eligible(self, mode: _PageMode) -> Optional[str]:
        if mode.kind != "image":
            return mode.reason or "error-level analysis needs an image original"
        if not mode.jpeg_original:
            return "original is not an unrotated JPEG, so it carries no JPEG compression history to compare"
        return None

    def _stage(self, img, cfg):
        return algo.ela_regions(img, cfg)

    def _signals(self, ctx, page, size, cfg, stage):
        out = []
        for n, r in enumerate(stage.regions, 1):
            region = _region(ctx, page.page, r.box, size, "residual outlier area")
            if region is None:
                continue
            conf = round(min(_ELA_MAX_CONFIDENCE, 0.2 + 0.01 * max(0.0, r.score - cfg.elaZThreshold)), 3)
            out.append(ForensicSignal(
                signalId=f"{ctx.doc_id}-ela-p{page.page}-{n:02d}", detector=self.name, type="ELA_ANOMALY",
                regions=[region], confidence=conf,
                measurements={"peakZ": round(r.score, 2), "blocks": int(r.measurements["blocks"]),
                              "blockSizePx": cfg.elaBlockPx, "jpegQuality": cfg.elaQuality},
                provenance=_provenance(self.name, self.version, ctx, cfg, {"page": page.page})))
        return out


@register_analyzer
class ImageNoiseAnalyzer(_ImageStageAnalyzer):
    name = "image.noise"
    stage_label = "noise-consistency test"

    def _stage(self, img, cfg):
        return algo.low_noise_regions(img, cfg)

    def _signals(self, ctx, page, size, cfg, stage):
        out = []
        for n, r in enumerate(stage.regions, 1):
            region = _region(ctx, page.page, r.box, size, "low-noise area")
            if region is None:
                continue
            conf = round(min(0.8, 0.4 + 0.05 * max(0.0, r.score - cfg.noiseLowZThreshold)), 3)
            out.append(ForensicSignal(
                signalId=f"{ctx.doc_id}-noise-p{page.page}-{n:02d}", detector=self.name, type="NOISE_INCONSISTENCY",
                regions=[region], confidence=conf,
                measurements={"lowNoiseZ": round(r.score, 2), "blocks": int(r.measurements["blocks"]),
                              "blockSizePx": cfg.noiseBlockPx,
                              "pageNoiseSd": round(stage.stats.get("noiseSdMedian", 0.0), 3),
                              "direction": "quieter_than_page"},
                provenance=_provenance(self.name, self.version, ctx, cfg, {"page": page.page})))
        return out


@register_analyzer
class ImageCopyMoveAnalyzer(_ImageStageAnalyzer):
    name = "image.copymove"
    stage_label = "copy-move analysis"

    def _stage(self, img, cfg):
        return algo.copy_move_pairs(img, cfg)

    def _signals(self, ctx, page, size, cfg, stage):
        out = []
        for n, p in enumerate(stage.regions, 1):
            a = _region(ctx, page.page, p.first, size, "matching region A")
            b = _region(ctx, page.page, p.second, size, "matching region B")
            if a is None or b is None:
                continue
            span = (1.0 - cfg.cmMinCoverage) or 1.0
            conf = 0.5 + 0.3 * min(1.0, (p.coverage - cfg.cmMinCoverage) / span)
            conf += 0.15 * min(1.0, p.matched_ink_px / (4 * cfg.cmMinMatchedInkFraction * size[0] * size[1]))
            out.append(ForensicSignal(
                signalId=f"{ctx.doc_id}-copymove-p{page.page}-{n:02d}", detector=self.name, type="COPY_MOVE",
                regions=[a, b], confidence=round(min(0.95, conf), 3),
                measurements={"offsetXFrac": round(p.dx / size[0], 5), "offsetYFrac": round(p.dy / size[1], 5),
                              "matchedInkPx": p.matched_ink_px, "lineCoverage": p.coverage,
                              "glyphComponents": p.glyph_components, "orbVotes": p.votes},
                provenance=_provenance(self.name, self.version, ctx, cfg, {"page": page.page})))
        return out
