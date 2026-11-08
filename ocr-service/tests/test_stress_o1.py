"""O1 regression tests found by stress testing (see STRESS_TEST_REPORT.md). One test per bug."""
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from app.config import Settings
from app.context import AnalysisContext
from app.main import create_app
from app.models import DOC_ID_MAX_LEN
from app.registry import AnalyzerRegistry
from tests.conftest import make_pdf_bytes, make_png_bytes


def _client(settings):
    return TestClient(create_app(settings=settings, registry=AnalyzerRegistry()), raise_server_exceptions=False)


# ---------------------------------------------------------------- render / input handling

def test_overlong_doc_id_is_rejected_not_500(settings):
    client = _client(settings)
    for n in (DOC_ID_MAX_LEN + 1, 300, 5000):
        assert client.post("/analyze", json={"docId": "a" * n}).status_code == 422
    assert client.post("/analyze", json={"docId": "a" * DOC_ID_MAX_LEN}).status_code == 404  # valid shape, no such document


def test_concurrent_analyses_of_one_document_all_succeed(settings, put_original):
    put_original("conc", "original.pdf", make_pdf_bytes(pages=3))
    client = _client(settings)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(client.post("/analyze", json={"docId": "conc"}).status_code))
               for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200] * 12, codes  # used to fail with 415 "failed to render PDF page N" (shared temp file name)
    assert not list((settings.derived_dir / "conc").glob("*.tmp"))


def test_multipage_tiff_renders_every_page(settings, put_original):
    frames = [Image.new("RGB", (80, 60), c) for c in ("white", "red", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    put_original("mt", "original.tif", buf.getvalue())
    body = _client(settings).post("/analyze", json={"docId": "mt"}).json()
    assert [p["page"] for p in body["pages"]] == [1, 2, 3]
    assert Image.open(settings.derived_dir / "mt" / "page-3.png").getpixel((5, 5)) == (0, 0, 255)


def test_png_with_pdf_marker_in_metadata_is_still_an_image(settings, put_original):
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "%PDF-1.4 not a pdf")
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG", pnginfo=info)
    put_original("poly", "original.png", buf.getvalue())
    assert _client(settings).post("/analyze", json={"docId": "poly"}).status_code == 200


def test_pdf_with_leading_junk_is_still_a_pdf(settings, put_original):
    put_original("junk", "original.pdf", b"\n\n% junk before the header\n" + make_pdf_bytes())
    assert _client(settings).post("/analyze", json={"docId": "junk"}).status_code in (200, 415)  # never 500


def test_total_pixel_budget_rejects_many_large_pages(tmp_path, put_original):
    small = Settings(data_dir=tmp_path / "data", render_dpi=100, analyzer_timeout_s=5.0,
                     max_image_pixels=1_000_000, max_total_pixels=1_500_000)
    folder = small.evidence_dir / "big"
    folder.mkdir(parents=True)
    (folder / "original.pdf").write_bytes(make_pdf_bytes(pages=3))  # A4 at 100 dpi is about 0.65 MP per page
    assert _client(small).post("/analyze", json={"docId": "big"}).status_code == 415


# ---------------------------------------------------------------- configuration

@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400", "0", "-5", "99999"])
def test_bad_analyzer_timeout_is_rejected_at_startup(monkeypatch, tmp_path, value):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANALYZER_TIMEOUT_S", value)
    with pytest.raises(ValueError):
        Settings.from_env()


# ---------------------------------------------------------------- analysis context

def test_hung_computation_does_not_block_other_keys(settings, put_original):
    put_original("c", "original.png", make_png_bytes())
    from app.render import render_document
    ctx = AnalysisContext(render_document("c", settings), settings)
    release = threading.Event()
    threading.Thread(target=lambda: ctx.get_or_compute("slow", lambda: release.wait(5)), daemon=True).start()
    time.sleep(0.1)
    start = time.monotonic()
    assert ctx.get_or_compute("other", lambda: 42) == 42
    assert time.monotonic() - start < 1.0
    release.set()


def test_get_or_compute_computes_once_under_contention(settings, put_original):
    put_original("c2", "original.png", make_png_bytes())
    from app.render import render_document
    ctx = AnalysisContext(render_document("c2", settings), settings)
    calls = []

    def factory():
        calls.append(1)
        time.sleep(0.05)
        return "v"

    results = []
    threads = [threading.Thread(target=lambda: results.append(ctx.get_or_compute("k", factory))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["v"] * 8 and len(calls) == 1


def test_failed_computation_is_not_cached(settings, put_original):
    put_original("c3", "original.png", make_png_bytes())
    from app.render import render_document
    ctx = AnalysisContext(render_document("c3", settings), settings)

    def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        ctx.get_or_compute("k", boom)
    assert ctx.get_or_compute("k", lambda: "ok") == "ok"
