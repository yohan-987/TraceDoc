"""C1 report (app/extract_report.py, tools/accuracy_report.py): behaviour is preserved, and the D1 manifest is not mixed in.

The C1 report compares recorded C1 OCR fixtures (D-01, D-02, D-03). Those are NOT D1 documents. The D1 manifest therefore
goes to tools/d1_accuracy_report.py, and this module refuses it instead of mis-parsing it.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from app import extract_report as rep

D1_SHAPE = {"files": [{"doc_id": "D1-ID-001", "doc_type": "id_card", "variant": "digital", "path": "d1-lite/a.png", "page": 1,
                       "fields": [{"name": "dob", "value": "1990-08-12", "bbox": [0.1, 0.1, 0.2, 0.05]}]}]}
TOOL = Path(__file__).resolve().parents[1] / "tools" / "accuracy_report.py"


def write(tmp_path, data, name="manifest.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_d1_manifest_shape_is_recognised_and_c1_shapes_are_not(tmp_path):
    assert rep.is_d1_manifest(write(tmp_path, D1_SHAPE))
    for c1 in ([{"docId": "D-01", "fields": {"dob": "2004-08-12"}}], {"documents": [{"id": "D-01", "fields": {}}]},
               {"D-01": {"fields": {"dob": "x"}}}):
        assert not rep.is_d1_manifest(write(tmp_path, c1, "c1.json"))
    assert not rep.is_d1_manifest(tmp_path / "missing.json")


def test_c1_report_refuses_the_d1_manifest_and_points_to_the_d1_tool(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    comparison, blockers = rep.accuracy_from_manifest(write(tmp_path, D1_SHAPE), fixtures)
    assert comparison is None
    text = " ".join(blockers)
    assert "D1 manifest" in text and "NOT D1 documents" in text and "tools/d1_accuracy_report.py" in text


def test_c1_loader_does_not_silently_accept_doc_id_entries(tmp_path):
    manifest = rep.load_manifest(write(tmp_path, D1_SHAPE))
    assert manifest.docs == {} and manifest.problems            # nothing is guessed from 'doc_id' entries


def test_c1_shapes_still_parse_exactly_as_before(tmp_path):
    for data in ([{"docId": "D-01", "docType": "id_card", "fields": {"dob": "2004-08-12"}}],
                 {"documents": [{"id": "D-01", "fields": [{"name": "dob", "normalizedValue": "2004-08-12"}]}]},
                 {"D-01": {"fields": {"dob": {"value": "2004-08-12", "page": 1}}}}):
        manifest = rep.load_manifest(write(tmp_path, data))
        assert manifest.docs["D-01"].fields["dob"].value == "2004-08-12"


def test_c1_comparison_and_tables_are_unchanged():
    manifest = rep.Manifest(docs={"D-01": rep.ManifestDoc("D-01", "id_card", {"dob": rep.Expected("2004-08-12", "value"),
                                                                              "id_number": rep.Expected("X-1", "value")})})
    found = SimpleNamespace(name="dob", rawValue="12/08/2004", normalizedValue="2004-08-12", fieldId="F1",
                            region=SimpleNamespace(page=1, bbox=(0.1, 0.1, 0.2, 0.05)))
    cmp = rep.compare(manifest, {"D-01": [found]}, {})
    assert {r.name: r.match for r in cmp.rows} == {"dob": True, "id_number": False}
    text = rep.format_report(cmp, "C1")
    assert "Document type | Doc" in text and "Accuracy %" in text and "<missing>" in text


def test_c1_tool_still_reports_missing_inputs_with_exit_code_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAMPLES_MANIFEST", str(tmp_path / "nope.json"))
    spec = importlib.util.spec_from_file_location("c1_cli", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 2 and "NOT COMPUTED" in capsys.readouterr().out
