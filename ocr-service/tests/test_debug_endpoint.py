"""GET /debug/ocr/{docId}/{page}: development-only overlay of OCR boxes on the canonical page image."""
import io

import pytest
from PIL import Image

from app import ocr, ocr_core
from app.config import Settings
from app.debug_draw import TEXT_LAYER, WORD_HIGH, WORD_LOW
from app.registry import AnalyzerRegistry
from tests.conftest import make_pdf_bytes, make_png_bytes
from tests.ocr_helpers import fake_tsv, text_pdf_bytes


@pytest.fixture
def fake_ocr(monkeypatch):
    """Mocked Tesseract returning two words on one line (the first at pixel (100, 50) of a 1000x500 page)."""
    state = {"conf": 96.0}

    def image_to_data(image, **kwargs):
        return fake_tsv([(1, 1, 1, 1, 100, 50, 200, 40, state["conf"], "Hello"),
                         (1, 1, 1, 2, 320, 50, 180, 40, 90.0, "World")])

    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", image_to_data)
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    return state


@pytest.fixture
def debug_client(make_client):
    return make_client(AnalyzerRegistry(), debug_endpoints=True)


def _image(response):
    return Image.open(io.BytesIO(response.content)).convert("RGB")


def test_returns_png_with_boxes_drawn(debug_client, settings, put_original, fake_ocr):
    put_original("DBG-1", "original.png", make_png_bytes((1000, 500)))
    r = debug_client.get("/debug/ocr/DBG-1/1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "blue" in r.headers["x-debug-legend"]
    img = _image(r)
    assert img.size == (1000, 500)  # the canonical page, same size
    assert img.getpixel((100, 50)) == WORD_HIGH  # top-left corner of the first word box
    canonical = Image.open(settings.derived_dir / "DBG-1" / "page-1.png").convert("RGB")
    assert list(img.getdata()) != list(canonical.getdata())  # boxes were drawn over the page
    assert canonical.getpixel((100, 50)) == (255, 255, 255)  # and the stored render is untouched


def test_low_confidence_words_are_drawn_differently(debug_client, put_original, fake_ocr):
    fake_ocr["conf"] = 30.0
    put_original("DBG-2", "original.png", make_png_bytes((1000, 500)))
    assert _image(debug_client.get("/debug/ocr/DBG-2/1")).getpixel((100, 50)) == WORD_LOW


def test_page_numbers_are_one_based(debug_client, put_original, fake_ocr):
    put_original("DBG-3", "original.pdf", make_pdf_bytes(pages=2))
    assert debug_client.get("/debug/ocr/DBG-3/1").status_code == 200
    assert debug_client.get("/debug/ocr/DBG-3/2").status_code == 200
    assert debug_client.get("/debug/ocr/DBG-3/3").status_code == 404
    assert debug_client.get("/debug/ocr/DBG-3/0").status_code == 422


def test_disabled_by_default(make_client, put_original, fake_ocr):
    put_original("DBG-4", "original.png", make_png_bytes((1000, 500)))
    client = make_client(AnalyzerRegistry())  # debug_endpoints defaults to False
    assert client.get("/debug/ocr/DBG-4/1").status_code == 404
    assert not any(p.startswith("/debug") for p in client.get("/openapi.json").json()["paths"])


def test_enabled_only_by_explicit_setting(monkeypatch):
    monkeypatch.setattr("app.config._load_dotenv", lambda path: None)  # ignore a local .env
    monkeypatch.delenv("DEBUG_ENDPOINTS", raising=False)
    assert Settings.from_env().debug_endpoints is False
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("DEBUG_ENDPOINTS", value)
        assert Settings.from_env().debug_endpoints is True
    monkeypatch.setenv("DEBUG_ENDPOINTS", "0")
    assert Settings.from_env().debug_endpoints is False


@pytest.mark.parametrize("path", ["/debug/ocr/bad.id/1", "/debug/ocr/DBG/abc", "/debug/ocr/DBG/-1",
                                  "/debug/ocr/DBG/0", "/debug/ocr/DBG/1?source=nonsense"])
def test_invalid_input_is_422(debug_client, fake_ocr, path):
    assert debug_client.get(path).status_code == 422


@pytest.mark.parametrize("doc_id", ["..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "..%5C..%5Csecret", "a%2Fb"])
def test_no_filesystem_access_through_the_id(debug_client, fake_ocr, doc_id):
    assert debug_client.get(f"/debug/ocr/{doc_id}/1").status_code in (404, 422)


def test_missing_document_is_404(debug_client, fake_ocr):
    assert debug_client.get("/debug/ocr/NOPE/1").status_code == 404


def test_unavailable_ocr_is_503(debug_client, put_original, monkeypatch):
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    put_original("DBG-5", "original.png", make_png_bytes((1000, 500)))
    r = debug_client.get("/debug/ocr/DBG-5/1")
    assert r.status_code == 503 and "unavailable" in r.json()["detail"].lower()


def test_unreadable_page_is_an_error_response(debug_client, put_original, fake_ocr, monkeypatch):
    put_original("DBG-6", "original.png", make_png_bytes((1000, 500)))
    from app import render
    real = render.render_document

    def corrupt(doc_id, settings):
        doc = real(doc_id, settings)
        doc.pages[0].path.write_bytes(b"corrupted")
        return doc

    monkeypatch.setattr("app.debug.render_document", corrupt)
    assert debug_client.get("/debug/ocr/DBG-6/1").status_code == 502


def test_text_layer_source_on_digital_pdf(debug_client, put_original):
    put_original("DBG-7", "original.pdf", text_pdf_bytes([["FICTIONAL INVOICE 12345"]]))
    r = debug_client.get("/debug/ocr/DBG-7/1?source=pdf_text_layer")  # needs no OCR engine
    assert r.status_code == 200
    assert TEXT_LAYER in {color for _, color in _image(r).getcolors(maxcolors=1 << 24)}


def test_text_layer_source_not_available_for_images(debug_client, put_original):
    put_original("DBG-8", "original.png", make_png_bytes((1000, 500)))
    assert debug_client.get("/debug/ocr/DBG-8/1?source=pdf_text_layer").status_code == 404
