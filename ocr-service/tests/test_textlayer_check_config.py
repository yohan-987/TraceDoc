"""O6 thresholds file: config/text_layer_check.json is the single source of truth and is validated on load."""
import json

import pytest

from app.config import SERVICE_ROOT
from app.textlayer_check_config import TextLayerCheckConfigError, load_text_layer_check_config


def shipped():
    return json.loads((SERVICE_ROOT / "config" / "text_layer_check.json").read_text())


def write(tmp_path, data=None, text=None):
    path = tmp_path / "c.json"
    path.write_text(text if text is not None else json.dumps(data))
    return path


def test_shipped_config_loads_and_is_echoed_into_provenance():
    cfg = load_text_layer_check_config()
    prov = cfg.as_provenance()
    assert prov["cfg.textSimilarityMin"] == cfg.textSimilarityMin and prov["cfg.flagRenderOnly"] == 0   # flags are 0 / 1, never JSON booleans
    assert all(isinstance(v, (int, float, str)) and not isinstance(v, bool) for v in prov.values())
    assert set(prov) == {f"cfg.{k}" for k in shipped()}


def test_override_path_via_environment(tmp_path, monkeypatch):
    data = shipped(); data["textSimilarityMin"] = 0.5
    monkeypatch.setenv("TEXT_LAYER_CHECK_CONFIG", str(write(tmp_path, data)))
    assert load_text_layer_check_config().textSimilarityMin == 0.5


@pytest.mark.parametrize("key,value", [("textSimilarityMin", float("nan")), ("hiddenConfidence", float("inf")), ("inkDelta", 300),
                                       ("minWordChars", 0), ("maxWordsPerPage", -1), ("minPageAgreement", 1.5),
                                       ("textSimilarityMin", 0.95), ("rereadScale", 9), ("rereadTimeoutS", 0),
                                       ("tinyHeightPx", 0), ("edgeMarginFrac", 0.5), ("hiddenConfidence", 0.99),
                                       ("candidatePadFrac", -0.1), ("maxRereadsPerPage", -1)])
def test_nonsensical_values_are_refused(tmp_path, key, value):
    data = shipped(); data[key] = value
    with pytest.raises(TextLayerCheckConfigError):
        load_text_layer_check_config(write(tmp_path, data))


@pytest.mark.parametrize("key,value", [("flagRenderOnly", 1), ("flagRenderOnly", "yes"), ("minWordChars", "2"), ("minWordChars", True),
                                       ("configVersion", 1), ("textSimilarityMin", None)])
def test_wrong_types_are_refused(tmp_path, key, value):
    data = shipped(); data[key] = value
    with pytest.raises(TextLayerCheckConfigError):
        load_text_layer_check_config(write(tmp_path, data))


def test_missing_unknown_and_unreadable_files_are_refused(tmp_path):
    data = shipped(); del data["inkDelta"]
    with pytest.raises(TextLayerCheckConfigError, match="inkDelta"):
        load_text_layer_check_config(write(tmp_path, data))
    data = shipped(); data["surprise"] = 1
    with pytest.raises(TextLayerCheckConfigError, match="surprise"):
        load_text_layer_check_config(write(tmp_path, data))
    for text in ("not json", "[]", ""):
        with pytest.raises(TextLayerCheckConfigError):
            load_text_layer_check_config(write(tmp_path, text=text))
    with pytest.raises(TextLayerCheckConfigError):
        load_text_layer_check_config(tmp_path / "missing.json")


def test_json_nan_literal_is_refused(tmp_path):
    text = json.dumps(shipped()).replace('"textSimilarityMin": 0.6', '"textSimilarityMin": NaN')
    with pytest.raises(TextLayerCheckConfigError):
        load_text_layer_check_config(write(tmp_path, text=text))
