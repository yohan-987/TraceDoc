"""Development-only debug endpoint: GET /debug/ocr/{docId}/{page}.

Registered ONLY when the explicit DEBUG_ENDPOINTS setting is on (off by default), so in production the
route does not exist (404). Input is a validated docId and a 1-based page number: no paths are accepted,
and the image served is always the canonical render of that document's page.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from PIL import Image

from .config import Settings
from .context import AnalysisContext
from .debug_draw import LEGEND, draw_boxes
from .models import DOC_ID_MAX_LEN, DOC_ID_PATTERN, InvalidDocIdError
from .ocr import OcrPageError, OcrUnavailableError, get_page_ocr
from .render import (
    AmbiguousOriginalError,
    DocumentNotFoundError,
    RenderError,
    UnsupportedDocumentError,
    render_document,
)
from .textlayer import TextLayerUnavailableError, get_text_layer_page


def _render_or_http(doc_id: str, settings: Settings):
    try:
        return render_document(doc_id, settings)
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


def register_debug_routes(app: FastAPI) -> None:
    @app.get("/debug/ocr/{docId}/{page}", response_class=Response,
             responses={200: {"content": {"image/png": {}}}})
    def debug_ocr(
        request: Request,
        docId: str = Path(pattern=rf"^{DOC_ID_PATTERN}$", max_length=DOC_ID_MAX_LEN),
        page: int = Path(ge=1, description="1-based page number"),
        source: Literal["ocr", "pdf_text_layer"] = Query("ocr", description="which words to draw"),
    ) -> Response:
        settings: Settings = request.app.state.settings
        document = _render_or_http(docId, settings)
        if page > len(document.pages):
            raise HTTPException(status_code=404, detail=f"page {page} not found (document has {len(document.pages)})")
        ctx = AnalysisContext(document, settings)
        try:
            if source == "ocr":
                words = get_page_ocr(ctx, page)
            else:
                words = get_text_layer_page(ctx, page).page
        except OcrUnavailableError as exc:
            raise HTTPException(status_code=503, detail=f"OCR unavailable: {exc}")
        except OcrPageError as exc:
            raise HTTPException(status_code=502, detail=f"OCR failed for this page: {exc}")
        except TextLayerUnavailableError as exc:
            raise HTTPException(status_code=404, detail=f"no PDF text layer available: {exc}")
        if source == "pdf_text_layer" and not words.words:
            raise HTTPException(status_code=404, detail="no PDF text layer available: this page has no text layer")
        with Image.open(document.pages[page - 1].path) as image:
            png = draw_boxes(image, words, text_layer=(source == "pdf_text_layer"))
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-store", "X-Debug-Legend": LEGEND})
