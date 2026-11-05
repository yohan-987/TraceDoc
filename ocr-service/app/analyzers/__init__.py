"""Analyzer plugins live here, one module per analyzer (added in later batches, O2 onward).

Every module in this package (except names starting with "_") is imported at startup. A plugin:

    from app.registry import Analyzer, register_analyzer
    from app.models import AnalyzerResult

    @register_analyzer
    class MyAnalyzer(Analyzer):
        name = "my.analyzer"
        version = "0.1"

        def applies_to(self, page): return True
        def run(self, ctx): return AnalyzerResult(status="AVAILABLE", signals=[...])

O1 ships no analyzers on purpose: an empty registry yields an empty, valid response.
"""
