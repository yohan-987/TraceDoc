import pytest
from PIL import Image

from app.render import (
    AmbiguousOriginalError,
    DocumentNotFoundError,
    UnsupportedDocumentError,
    render_document,
)
from tests.conftest import make_pdf_bytes, make_png_bytes

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_pdf_rendered_to_png_pages(settings, put_original):
    put_original("DOC-1", "original.pdf", make_pdf_bytes(pages=2))
    doc = render_document("DOC-1", settings)
    assert [p.page for p in doc.pages] == [1, 2]  # 1-based
    for p in doc.pages:
        expected = settings.derived_dir / "DOC-1" / f"page-{p.page}.png"
        assert p.path == expected and expected.read_bytes().startswith(PNG_MAGIC)
        assert p.record.renderPath == f"derived/DOC-1/page-{p.page}.png"
        assert p.record.renderDpi == settings.render_dpi
        assert p.record.rotationDeg == 0 and p.record.deskewDeg == 0
        assert p.record.docType == "UNKNOWN"
    # A4 at 100 dpi is about 827 x 1169 px
    assert abs(doc.pages[0].width_px - 827) <= 2 and abs(doc.pages[0].height_px - 1169) <= 2
    assert len(doc.input_sha256) == 64


def test_pdf_page_rotation_recorded(settings, put_original):
    put_original("DOC-R", "original.pdf", make_pdf_bytes(rotate=90))
    page = render_document("DOC-R", settings).pages[0]
    assert page.record.rotationDeg == 90
    assert page.width_px > page.height_px  # landscape after rotation


def test_png_accepted_and_saved(settings, put_original):
    put_original("IMG-1", "original.png", make_png_bytes((200, 120)))
    doc = render_document("IMG-1", settings)
    assert len(doc.pages) == 1
    path = settings.derived_dir / "IMG-1" / "page-1.png"
    assert path.is_file()
    with Image.open(path) as img:
        assert img.size == (200, 120)  # native pixels, not resampled


def test_exif_orientation_corrected(settings, put_original, tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # needs 90 degrees clockwise
    src = tmp_path / "src.jpg"
    Image.new("RGB", (100, 50), "white").save(src, exif=exif)
    put_original("IMG-EXIF", "original.jpg", src.read_bytes())
    page = render_document("IMG-EXIF", settings).pages[0]
    assert (page.width_px, page.height_px) == (50, 100)
    assert page.record.rotationDeg == 90


def test_rerender_removes_stale_pages(settings, put_original):
    path = put_original("DOC-S", "original.pdf", make_pdf_bytes(pages=3))
    render_document("DOC-S", settings)
    path.write_bytes(make_pdf_bytes(pages=1))
    render_document("DOC-S", settings)
    out = settings.derived_dir / "DOC-S"
    assert sorted(p.name for p in out.glob("page-*.png")) == ["page-1.png"]


def test_missing_document(settings):
    with pytest.raises(DocumentNotFoundError):
        render_document("NOPE", settings)


def test_ambiguous_original(settings, put_original):
    put_original("DUP", "original.pdf", make_pdf_bytes())
    put_original("DUP", "original.png", make_png_bytes())
    with pytest.raises(AmbiguousOriginalError):
        render_document("DUP", settings)


def test_garbage_file_unsupported(settings, put_original):
    put_original("BAD", "original.bin", b"not a document at all")
    with pytest.raises(UnsupportedDocumentError):
        render_document("BAD", settings)


def test_image_over_the_pixel_limit_is_rejected_before_decoding(settings, put_original):
    from app.config import Settings
    small = Settings(**{**settings.__dict__, "max_image_pixels": 10_000})
    put_original("BIG-IMG", "original.png", make_png_bytes((200, 120)))      # 24,000 px > 10,000
    with pytest.raises(UnsupportedDocumentError) as info:
        render_document("BIG-IMG", small)
    assert "too large" in str(info.value)
    assert not (small.derived_dir / "BIG-IMG").exists()                      # nothing was written


def test_pdf_page_over_the_pixel_limit_is_rejected(settings, put_original):
    from app.config import Settings
    small = Settings(**{**settings.__dict__, "max_image_pixels": 100_000})   # A4 at 100 dpi is about 967,000 px
    put_original("BIG-PDF", "original.pdf", make_pdf_bytes(pages=1))
    with pytest.raises(UnsupportedDocumentError):
        render_document("BIG-PDF", small)


def test_pixel_limit_comes_from_the_environment(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("MAX_IMAGE_PIXELS", "1234")
    assert Settings.from_env().max_image_pixels == 1234
    monkeypatch.setenv("MAX_IMAGE_PIXELS", "0")
    with pytest.raises(ValueError):
        Settings.from_env()
