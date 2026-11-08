"""O3 regression tests found by stress testing (see STRESS_TEST_REPORT.md)."""
import json

import pytest

from app.classifier_config import ClassifierConfigError, load_classifier_config
from app.config import SERVICE_ROOT


def _classifier_json(**changes):
    base = json.loads((SERVICE_ROOT / "config" / "classifier.json").read_text())
    base.update(changes)
    return json.dumps(base)  # real float('nan') / float('inf') are written as NaN / Infinity, which json.loads accepts


@pytest.mark.parametrize("key,value", [("scoreScale", float("nan")), ("scoreScale", float("inf")), ("frequencyBonus", float("nan")),
                                       ("comboBonusPerKeyword", float("inf")), ("frequencyCap", 0), ("comboFreeKeywords", -1),
                                       ("transactionRowCap", 0), ("formLabelLineRatioMin", 2)])
def test_classifier_config_rejects_nonsense(tmp_path, key, value):
    path = tmp_path / "c.json"
    path.write_text(_classifier_json(**{key: value}))
    with pytest.raises(ClassifierConfigError):
        load_classifier_config(path)
