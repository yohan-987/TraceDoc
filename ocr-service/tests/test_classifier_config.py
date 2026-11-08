"""Classifier thresholds live in config/classifier.json and are validated on load."""
import json
from dataclasses import fields

import pytest

from app.classifier_config import ClassifierConfig, ClassifierConfigError, DEFAULT_PATH, load_classifier_config


def _write(tmp_path, **overrides):
    data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    data.update(overrides)
    path = tmp_path / "classifier.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_default_file_loads_and_is_complete():
    cfg = load_classifier_config()
    assert 0 < cfg.minScore < 1 and 0 <= cfg.ambiguityMargin < 1
    on_disk = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    assert set(on_disk) == {f.name for f in fields(ClassifierConfig)}  # no value hidden in code


def test_as_provenance_echoes_every_threshold():
    cfg = load_classifier_config()
    echoed = cfg.as_provenance()
    assert set(echoed) == {f"cfg.{f.name}" for f in fields(ClassifierConfig)}
    assert echoed["cfg.minScore"] == cfg.minScore and echoed["cfg.ambiguityMargin"] == cfg.ambiguityMargin
    assert all(isinstance(v, (int, float, str)) for v in echoed.values())  # contract Scalar


def test_env_override_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_CONFIG", str(_write(tmp_path, minScore=0.77)))
    assert load_classifier_config().minScore == 0.77


@pytest.mark.parametrize("overrides", [{"minScore": 0}, {"minScore": 1.5}, {"scoreScale": 0}, {"ambiguityMargin": -0.1},
                                       {"minDistinctKeywords": 0}, {"idCardAspectMin": 9.0}, {"minScore": "high"},
                                       {"frequencyCap": 2.5}])
def test_invalid_values_are_rejected(tmp_path, overrides):
    with pytest.raises(ClassifierConfigError):
        load_classifier_config(_write(tmp_path, **overrides))


def test_missing_and_unknown_keys_are_rejected(tmp_path):
    data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["minScore"]
    data["surprise"] = 1
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ClassifierConfigError) as info:
        load_classifier_config(path)
    assert "minScore" in str(info.value) and "surprise" in str(info.value)


def test_missing_or_broken_file(tmp_path):
    with pytest.raises(ClassifierConfigError):
        load_classifier_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClassifierConfigError):
        load_classifier_config(bad)
