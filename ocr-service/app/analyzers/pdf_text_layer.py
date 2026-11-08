"""PDF text-layer word extraction (registered as "pdf.text_layer_words").

Like the OCR analyzer it adds no fields or signals; it fills the shared context (app/textlayer.py) and
reports status plus provenance. Not applicable to image originals. A PDF with no text layer (typically
scanned) is AVAILABLE with zero words: that says nothing about whether the document is genuine.
"""
from __future__ import annotations

from ..context import AnalysisContext
from ..models import AnalyzerResult
from ..registry import Analyzer, register_analyzer
from ..textlayer import SOURCE, get_text_layer


@register_analyzer
class PdfTextLayerWordsAnalyzer(Analyzer):
    name = "pdf.text_layer_words"
    version = "0.1"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        result = get_text_layer(ctx)
        if result.not_pdf:
            return AnalyzerResult.not_applicable("original is not a PDF")
        if result.error:
            return AnalyzerResult(status="ERROR", reason=result.error)
        words = sum(len(p.page.words) for p in result.pages.values())
        with_text = sum(1 for p in result.pages.values() if p.page.words)
        config = {"source": SOURCE, "pages": len(ctx.pages), "pagesWithText": with_text, "words": words}
        model_version = next((p.page.engineVersion for p in result.pages.values()), None)
        if result.errors:
            config["pagesFailed"] = ",".join(str(n) for n in sorted(result.errors))
            return AnalyzerResult(status="ERROR", reason="text-layer extraction failed on some pages",
                                  model_version=model_version, config=config)
        reason = "PDF has no text layer" if words == 0 else None
        return AnalyzerResult(status="AVAILABLE", reason=reason, model_version=model_version, config=config)
