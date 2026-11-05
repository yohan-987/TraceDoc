"""FastAPI application: GET /health and POST /analyze."""
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .context import AnalysisContext
from .models import AnalyzeRequest, AnalyzeResponse, InvalidDocIdError
from .registry import AnalyzerRegistry, default_registry, discover_analyzers, run_analyzers
from .render import (
    AmbiguousOriginalError,
    DocumentNotFoundError,
    RenderError,
    UnsupportedDocumentError,
    render_document,
)

logger = logging.getLogger("ocr-service")


def create_app(settings: Optional[Settings] = None, registry: Optional[AnalyzerRegistry] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if registry is None:
        registry = discover_analyzers()  # default registry, plugins import themselves
    app = FastAPI(title="TraceDocs analysis service", version=settings.pipeline_version)
    app.state.settings = settings
    app.state.registry = registry

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "ocr-service", "pipelineVersion": settings.pipeline_version,
                "analyzersRegistered": len(registry)}

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(body: AnalyzeRequest, request: Request) -> JSONResponse:
        cfg: Settings = request.app.state.settings
        reg: AnalyzerRegistry = request.app.state.registry
        try:
            document = render_document(body.docId, cfg)
        except InvalidDocIdError:
            raise HTTPException(status_code=422, detail="docId must match [A-Za-z0-9_-]+")
        except DocumentNotFoundError:
            raise HTTPException(status_code=404, detail="document not found")
        except AmbiguousOriginalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except UnsupportedDocumentError as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except RenderError as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail=str(exc))

        ctx = AnalysisContext(document, cfg)
        outcome = run_analyzers(ctx, reg, cfg.analyzer_timeout_s)
        response = AnalyzeResponse(
            docId=document.doc_id,
            pipelineVersion=cfg.pipeline_version,
            pages=[p.record for p in document.pages],
            fields=outcome.fields,
            signals=outcome.signals,
            analyzerStatuses=outcome.statuses,
            provenance=outcome.provenance,
        )
        # exclude_unset keeps optional keys (e.g. docTypeOverriddenBy) absent instead of null,
        # while explicit nulls such as normalizedValue stay, as the JSON schema requires.
        return JSONResponse(response.model_dump(mode="json", exclude_unset=True))

    return app


app = create_app()
