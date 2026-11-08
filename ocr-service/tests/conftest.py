"""Shared fixtures. Every document is fictional and generated in a temp directory."""
import io

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import Settings
from app.main import create_app
from app.registry import AnalyzerRegistry


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", render_dpi=100, analyzer_timeout_s=5.0)


def make_pdf_bytes(pages: int = 1, rotate: int = 0) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4 points
        page.insert_text((72, 72), f"FICTIONAL TEST DOCUMENT page {i + 1}")
        if rotate:
            page.set_rotation(rotate)
    data = doc.tobytes()
    doc.close()
    return data


def make_png_bytes(size=(200, 120)) -> bytes:
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((10, 10), "FICTIONAL", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def put_original(settings):
    def _put(doc_id: str, filename: str, data: bytes):
        folder = settings.evidence_dir / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_bytes(data)
        return folder / filename
    return _put


@pytest.fixture
def empty_registry():
    return AnalyzerRegistry()


@pytest.fixture
def make_client(settings):
    def _make(registry, **overrides):
        cfg = Settings(**{**settings.__dict__, **overrides}) if overrides else settings
        return TestClient(create_app(settings=cfg, registry=registry))
    return _make


@pytest.fixture
def client(make_client, empty_registry):
    return make_client(empty_registry)


@pytest.fixture(autouse=True)
def _default_ocr_engine(monkeypatch):
    """Tests use Tesseract unless they opt in to PaddleOCR, so a locally installed PaddleOCR never
    changes the results of tests that mock or expect Tesseract."""
    from app import ocr_paddle
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    ocr_paddle.reset_paddle()
    yield
    ocr_paddle.reset_paddle()
