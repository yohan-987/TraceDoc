"""How PaddleOCR is constructed (a fake `paddleocr` module records the arguments; no real PaddleOCR needed)."""
import sys
import types

import pytest

from app import ocr_paddle


def fake_module(accept_v3=True):
    created = []

    class PaddleOCR:
        def __init__(self, **kwargs):
            if not accept_v3 and "use_doc_orientation_classify" in kwargs:
                raise ValueError("unknown argument")
            created.append(kwargs)

    return types.SimpleNamespace(PaddleOCR=PaddleOCR, __version__="3.7.0"), created


def test_onednn_is_disabled_by_default(monkeypatch):
    module, created = fake_module()
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.delenv("PADDLE_ENABLE_MKLDNN", raising=False)
    _, version = ocr_paddle._create_engine()
    assert version == "3.7.0"
    assert created[0]["enable_mkldnn"] is False
    assert created[0]["lang"] == "en"


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("0", False), ("", False), ("off", False)])
def test_onednn_can_be_enabled_explicitly(monkeypatch, value, expected):
    module, created = fake_module()
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.setenv("PADDLE_ENABLE_MKLDNN", value)
    ocr_paddle._create_engine()
    assert created[0]["enable_mkldnn"] is expected


def test_onednn_is_disabled_for_the_2x_api_too(monkeypatch):
    module, created = fake_module(accept_v3=False)
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.delenv("PADDLE_ENABLE_MKLDNN", raising=False)
    ocr_paddle._create_engine()
    assert created[0]["enable_mkldnn"] is False and created[0]["use_angle_cls"] is False


def test_missing_paddleocr_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)  # makes `import paddleocr` raise ImportError
    with pytest.raises(ocr_paddle.PaddleUnavailable):
        ocr_paddle._create_engine()


def test_the_known_oneDNN_error_is_reported_with_a_hint(monkeypatch, tmp_path):
    from PIL import Image

    class Broken:
        def predict(self, path):
            raise NotImplementedError("(Unimplemented) ConvertPirAttribute2RuntimeAttribute not support "
                                      "[pir::ArrayAttribute<pir::DoubleAttribute>]")

    image = tmp_path / "page-1.png"
    Image.new("RGB", (50, 50), "white").save(image)
    monkeypatch.setattr(ocr_paddle, "_create_engine", lambda: (Broken(), "3.7.0"))
    monkeypatch.setenv("PADDLE_ENABLE_MKLDNN", "0")
    with pytest.raises(ocr_paddle.PaddleFailure) as info:
        ocr_paddle.ocr_image_file(image)
    assert "ConvertPirAttribute2RuntimeAttribute" in str(info.value) and "README" in str(info.value)
