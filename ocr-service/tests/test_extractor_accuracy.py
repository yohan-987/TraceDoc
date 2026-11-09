"""Per-field accuracy report.

* The REAL comparison uses samples/manifest.json (expected values) and contracts/fixtures/ocr/ (recorded OCR). When either
  is missing the test is skipped with the exact reason, and nothing is compared or claimed.
* A SYNTHETIC regression table (self-authored pages) is printed too; it is labelled as such and is NOT the manifest.
* The report helpers are unit-tested with fabricated data (never presented as results).
"""
import json
from types import SimpleNamespace

import pytest

from app import extract_report as rep
from app.ocr_fixtures import fixtures_dir
from tests import extractor_pages as X


def emit(capsys, text):
    with capsys.disabled():
        print("\n" + text + "\n")


def field(name, raw, normalized, page=1, bbox=(0.1, 0.1, 0.2, 0.05), fid=None):
    return SimpleNamespace(name=name, rawValue=raw, normalizedValue=normalized, fieldId=fid or f"FLD-{name}",
                           region=SimpleNamespace(page=page, bbox=bbox))


# ----- real manifest + recorded fixtures

def test_manifest_accuracy_report(capsys):
    manifest = rep.find_manifest()
    comparison, blockers = rep.accuracy_from_manifest(manifest, fixtures_dir())
    if comparison is None:
        emit(capsys, "=== MANIFEST ACCURACY: NOT PERFORMED ===\n" + "\n".join(f"  - {b}" for b in blockers))
        pytest.skip("manifest accuracy cannot be computed: " + "; ".join(blockers))
    emit(capsys, rep.format_report(comparison, "ACCURACY vs samples/manifest.json (recorded OCR fixtures)", blockers))
    if not comparison.rows and comparison.cannot_compare:
        # Every document lacked recorded OCR: this is the D1 manifest (real files, no OCR fixtures), which the D1
        # evaluator scores by running the pipeline. Do not fail the C1 comparison for data it can never have.
        pytest.skip("no recorded OCR fixtures for the manifest's documents (D1 manifest); see tools/d1_accuracy_report.py")
    assert comparison.rows, "the manifest produced no comparable fields"
    wrong = [f"{r.docId}.{r.name}: expected {r.expected!r}, extracted {r.extracted!r}" for r in comparison.rows if not r.match]
    assert not wrong, "field-level exact match failed:\n  " + "\n  ".join(wrong)


# ----- synthetic regression table (NOT the manifest)

def test_synthetic_pages_accuracy_table(capsys):
    docs = {"SYN-ID": {1: X.id_card(doc_id="SYN-ID")}, "SYN-FORM": {1: X.application_form(doc_id="SYN-FORM")},
            "SYN-STMT": {1: X.bank_statement(doc_id="SYN-STMT")}}
    extracted, doc_types = rep.extract_from_ocr_pages(docs)
    expected = {"SYN-ID": X.ID_EXPECTED, "SYN-FORM": X.FORM_EXPECTED, "SYN-STMT": X.STATEMENT_EXPECTED}
    manifest = rep.Manifest(docs={d: rep.ManifestDoc(d, None, {k: rep.Expected(v, "normalizedValue") for k, v in e.items()})
                                  for d, e in expected.items()})
    comparison = rep.compare(manifest, extracted, doc_types)
    emit(capsys, rep.format_report(comparison, "SYNTHETIC PAGES (self-authored expectations; NOT samples/manifest.json)"))
    assert doc_types == {"SYN-ID": "id_card", "SYN-FORM": "application_form", "SYN-STMT": "bank_statement"}
    assert all(r.match for r in comparison.rows) and len(comparison.rows) == 16
    assert [u[2] for u in comparison.unexpected] == ["signature"]      # extracted but not expected: reported separately


# ----- when inputs are missing nothing is faked

def test_missing_manifest_is_reported_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMPLES_MANIFEST", str(tmp_path / "manifest.json"))
    assert rep.find_manifest() is None
    comparison, blockers = rep.accuracy_from_manifest(rep.find_manifest(), None)
    assert comparison is None and "was not found" in blockers[0]
    assert not (tmp_path / "manifest.json").exists()                    # nothing was created


def test_manifest_without_expected_values_cannot_be_compared(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"documents": [{"docId": "D-01", "fields": {}}, {"docId": "D-02"}]}), encoding="utf-8")
    comparison, blockers = rep.accuracy_from_manifest(path, tmp_path)
    assert comparison is None and any("no usable documents" in b for b in blockers)


def test_missing_fixtures_directory_is_reported(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([{"docId": "D-01", "fields": {"dob": "2004-08-12"}}]), encoding="utf-8")
    comparison, blockers = rep.accuracy_from_manifest(path, None)
    assert comparison is None and any("fixtures" in b for b in blockers)


def test_document_without_recorded_ocr_is_listed_as_not_compared(tmp_path):
    manifest = rep.Manifest(docs={"D-09": rep.ManifestDoc("D-09", "id_card", {"dob": rep.Expected("2004-08-12", "value")})})
    cmp = rep.compare(manifest, {}, {})
    assert cmp.rows == [] and "not compared" in cmp.cannot_compare[0]


# ----- comparison and formatting (fabricated data, only to test the helpers)

def test_missing_extracted_field_counts_as_a_mismatch_and_unexpected_is_separate():
    manifest = rep.Manifest(docs={"D-01": rep.ManifestDoc("D-01", "id_card", {
        "dob": rep.Expected("2004-08-12", "value"), "id_number": rep.Expected("X-1", "value")})})
    found = {"D-01": [field("dob", "12/08/2004", "2004-08-12"), field("full_name", "A B", "A B")]}
    cmp = rep.compare(manifest, found, {})
    rows = {r.name: r for r in cmp.rows}
    assert rows["dob"].match and not rows["id_number"].match and rows["id_number"].extracted == "<missing>"
    assert cmp.unexpected == [("id_card", "D-01", "full_name", "A B")]
    assert rep.accuracy(cmp) == {("id_card", "dob"): (1, 1), ("id_card", "id_number"): (0, 1)}


def test_expected_kinds_compare_against_the_right_extracted_value():
    f = field("dob", "12/08/2004", "2004-08-12")
    for kind, value, ok in (("normalizedValue", "2004-08-12", True), ("rawValue", "12/08/2004", True),
                            ("rawValue", "2004-08-12", False), ("value", "2004-08-12", True), ("value", "12/08/2004", False)):
        manifest = rep.Manifest(docs={"D": rep.ManifestDoc("D", "id_card", {"dob": rep.Expected(value, kind)})})
        assert rep.compare(manifest, {"D": [f]}, {}).rows[0].match is ok, (kind, value)


def test_report_tables_have_the_requested_columns():
    manifest = rep.Manifest(docs={"D-01": rep.ManifestDoc("D-01", "id_card", {"dob": rep.Expected("2004-08-12", "value"),
                                                                              "id_number": rep.Expected("X-1", "value")})})
    cmp = rep.compare(manifest, {"D-01": [field("dob", "12/08/2004", "2004-08-12")]}, {})
    text = rep.format_report(cmp, "TEST")
    assert "Document type | Doc" in text and "Expected" in text and "Extracted" in text and "Match" in text
    assert "Document type | Field" in text and "Correct" in text and "Total" in text and "Accuracy %" in text
    assert "MISMATCH" in text and "50.0" in text and "Unexpected extracted fields" in text
    assert "Comparisons that could not be performed" in text


def test_manifest_loader_accepts_the_documented_shapes(tmp_path):
    shapes = [
        [{"docId": "D-01", "docType": "id_card", "fields": {"dob": "2004-08-12"}}],
        {"documents": [{"id": "D-01", "fields": [{"name": "dob", "normalizedValue": "2004-08-12", "bbox": [0.1, 0.1, 0.2, 0.05]}]}]},
        {"D-01": {"fields": {"dob": {"value": "2004-08-12", "page": 1}}}},
    ]
    for i, shape in enumerate(shapes):
        path = tmp_path / f"m{i}.json"
        path.write_text(json.dumps(shape), encoding="utf-8")
        manifest = rep.load_manifest(path)
        assert manifest.docs["D-01"].fields["dob"].value == "2004-08-12", shape
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert rep.load_manifest(broken).problems
