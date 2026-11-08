"""O2b regression tests found by stress testing (see STRESS_TEST_REPORT.md)."""
import io

from PIL import Image, PngImagePlugin

from app.render import looks_like_pdf
from app.textlayer import is_pdf
from tests.conftest import make_pdf_bytes


def _png_with_pdf_marker() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "%PDF-1.4 not a pdf")
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def test_text_layer_and_renderer_agree_on_what_a_pdf_is(tmp_path):
    cases = {"poly.png": (_png_with_pdf_marker(), False), "plain.pdf": (make_pdf_bytes(), True),
             "junk.pdf": (b"\n\n% junk before the header\n" + make_pdf_bytes(), True)}
    for name, (data, expected) in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert is_pdf(path) is expected and looks_like_pdf(path) is expected, name  # one shared test: they cannot disagree


def test_debug_endpoint_rejects_overlong_doc_id(make_client):
    from app.registry import AnalyzerRegistry
    client = make_client(AnalyzerRegistry(), debug_endpoints=True)
    assert client.get("/debug/ocr/" + "a" * 300 + "/1").status_code == 422  # used to be a 500 (file name too long)
    assert client.get("/debug/ocr/nosuchdoc/1").status_code == 404
