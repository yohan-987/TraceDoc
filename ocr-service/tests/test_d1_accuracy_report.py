"""D1 accuracy evaluator (app/d1_eval.py, tools/d1_accuracy_report.py).

IMPORTANT: every manifest and document in this file is SYNTHETIC test data shaped like the D1 manifest. These tests check the
evaluator's logic; they say nothing about accuracy on the real D1 samples, which only
`python tools/d1_accuracy_report.py` against the real ../samples/manifest.json can report.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import d1_eval as d1
from app import ocr, ocr_core
from tests import extractor_pages as X
from tests.conftest import make_png_bytes
from tests.test_classifier_analyzer import tsv_from_page

CLI_PATH = Path(__file__).resolve().parents[1] / "tools" / "d1_accuracy_report.py"


# ----------------------------------------------------------------------------- helpers

def entry(doc_id, doc_type, variant, path, fields, page=1):
    return {"doc_id": doc_id, "doc_type": doc_type, "variant": variant, "path": path, "page": page, "fields": fields}


def f(name, value, bbox=(0.10, 0.10, 0.20, 0.05), page=None):
    out = {"name": name, "value": value, "bbox": list(bbox) if bbox is not None else None}
    if page:
        out["page"] = page
    return out


def synthetic_manifest(tmp_path, files=None):
    """A six-entry manifest shaped like D1 (digital + scan share each doc_id) with placeholder files."""
    folder = tmp_path / "samples"
    (folder / "d1-lite").mkdir(parents=True, exist_ok=True)
    spec = [("D1-ID-001", "id_card", "digital", "png"), ("D1-ID-001", "id_card", "scan", "jpg"),
            ("D1-APP-001", "application_form", "digital", "pdf"), ("D1-APP-001", "application_form", "scan", "jpg"),
            ("D1-BANK-001", "bank_statement", "digital", "pdf"), ("D1-BANK-001", "bank_statement", "scan", "jpg")]
    entries = []
    for doc_id, doc_type, variant, ext in spec:
        rel = f"d1-lite/{doc_id.lower()}_{variant}.{ext}"
        (folder / rel).write_bytes(b"placeholder")
        fields = [f("dob", "1990-08-12")] if doc_type == "id_card" else \
            [f("full_name", "John Q Sample"), f("statement_total", "3324.70")] if doc_type == "bank_statement" else \
            [f("full_name", "John Q Sample"), f("signature", "")]
        if doc_type == "bank_statement":
            fields += [f("txn_amount", v, (0.1, 0.4 + i * 0.05, 0.1, 0.03)) for i, v in enumerate(["100.00", "200.00", "300.00"])]
        entries.append(entry(doc_id, doc_type, variant, rel, fields))
    path = folder / "manifest.json"
    path.write_text(json.dumps({"files": files if files is not None else entries}), encoding="utf-8")
    return path


def ext(name, raw, norm=None, page=1, bbox=(0.10, 0.10, 0.20, 0.05), fid=None):
    return SimpleNamespace(name=name, rawValue=raw, normalizedValue=norm, fieldId=fid or f"FLD-{name}-{raw}",
                           region=SimpleNamespace(page=page, bbox=bbox))


def sample(fields, variant="digital", doc_type="bank_statement", doc_id="D1-X"):
    return d1.D1Sample(f"{doc_id}__{variant}", doc_id, doc_type, variant, "x.png", Path("x.png"), 1, tuple(fields))


def exp(name, value, order=0, bbox=None, page=1, warning=None):
    return d1.ExpectedField(name, value, bbox, page, order, warning)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- manifest parsing

def test_d1_manifest_is_parsed_with_files_and_fields(tmp_path):
    path = synthetic_manifest(tmp_path)
    before = sha(path)
    manifest = d1.parse_d1_manifest(path)
    assert manifest.problems == [] and len(manifest.samples) == 6
    s = manifest.samples[0]
    assert (s.doc_id, s.doc_type, s.variant, s.page, s.rel_path) == ("D1-ID-001", "id_card", "digital", 1, "d1-lite/d1-id-001_digital.png")
    assert s.abs_path and s.abs_path.is_file() and s.problems == ()
    assert [(e.name, e.value) for e in s.fields] == [("dob", "1990-08-12")]
    assert sha(path) == before                                           # the manifest is only read


def test_digital_and_scan_with_one_doc_id_get_separate_runtime_ids(tmp_path):
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path))
    ids = [s.runtime_id for s in manifest.samples]
    assert len(set(ids)) == 6
    assert "D1-ID-001__digital" in ids and "D1-ID-001__scan" in ids
    by_id = [s for s in manifest.samples if s.doc_id == "D1-ID-001"]
    assert {s.variant for s in by_id} == {"digital", "scan"}              # neither overwrote the other
    assert all(s.doc_id == "D1-ID-001" for s in by_id)                    # the manifest doc_id is preserved


def test_identical_doc_id_and_variant_still_get_distinct_runtime_ids(tmp_path):
    files = [entry("D1-A", "id_card", "scan", "d1-lite/a.jpg", [f("dob", "x")]),
             entry("D1-A", "id_card", "scan", "d1-lite/a.jpg", [f("dob", "y")])]
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path, files))
    assert [s.runtime_id for s in manifest.samples] == ["D1-A__scan", "D1-A__scan__2"]
    assert [s.fields[0].value for s in manifest.samples] == ["x", "y"]


def test_runtime_ids_are_filesystem_and_docid_safe(tmp_path):
    files = [entry("weird id/../x", "id_card", "sc an", "d1-lite/a.jpg", [f("dob", "x")])]
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path, files))
    from app.models import validate_doc_id
    assert validate_doc_id(manifest.samples[0].runtime_id) == manifest.samples[0].runtime_id


def test_repeated_txn_amount_values_are_all_preserved_in_order(tmp_path):
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path))
    bank = next(s for s in manifest.samples if s.doc_type == "bank_statement")
    txn = [e for e in bank.fields if e.name == "txn_amount"]
    assert [e.value for e in txn] == ["100.00", "200.00", "300.00"]
    assert [e.order for e in txn] == sorted(e.order for e in txn) and len({e.order for e in txn}) == 3


def test_bounding_boxes_and_pages_are_retained(tmp_path):
    files = [entry("D1-A", "id_card", "digital", "d1-lite/a.png",
                   [f("dob", "x", (0.25, 0.5, 0.125, 0.0625)), f("full_name", "y", (0.1, 0.1, 0.2, 0.05), page=2),
                    f("id_number", "z", None)], page=1)]
    s = d1.parse_d1_manifest(synthetic_manifest(tmp_path, files)).samples[0]
    assert s.fields[0].bbox == (0.25, 0.5, 0.125, 0.0625) and s.fields[0].page == 1
    assert s.fields[1].page == 2                                         # a per-field page overrides the file page
    assert s.fields[2].bbox is None


def test_suspicious_boxes_are_flagged_and_not_used_for_iou(tmp_path):
    files = [entry("D1-A", "id_card", "digital", "d1-lite/a.png",
                   [f("dob", "x", (0.2, 0.2, 0.9, 0.9)), {"name": "full_name", "value": "y", "bbox": [1, 2, 3]}])]
    a, b = d1.parse_d1_manifest(synthetic_manifest(tmp_path, files)).samples[0].fields
    assert a.bbox_warning and "IoU skipped" in a.bbox_warning and b.bbox is None
    assert d1._pair_iou(a, ext("dob", "x")) is None


def test_malformed_entries_and_fields_are_listed_not_guessed(tmp_path):
    files = [{"doc_type": "id_card", "path": "d1-lite/a.png"}, "nope",
             entry("D1-B", "id_card", "digital", "d1-lite/b.png", [{"name": "dob"}, {"value": "x"}, f("full_name", "ok")])]
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path, files))
    assert len(manifest.samples) == 1 and [e.name for e in manifest.samples[0].fields] == ["full_name"]
    text = " ".join(manifest.problems + list(manifest.samples[0].problems))
    assert "missing" in text and "not an object" in text and "needs a name and a value" in text


def test_paths_resolve_inside_the_samples_folder_only(tmp_path):
    (tmp_path / "secret.png").write_bytes(b"x")
    files = [entry("D1-A", "id_card", "digital", "../secret.png", []),
             entry("D1-B", "id_card", "digital", "/etc/passwd", []),
             entry("D1-C", "id_card", "digital", "d1-lite/missing.png", []),
             entry("D1-D", "id_card", "digital", "samples/d1-lite/d1-id-001_digital.png", [])]
    path = synthetic_manifest(tmp_path, files)
    samples = d1.parse_d1_manifest(path).samples
    assert samples[0].abs_path is None and any("escapes" in p for p in samples[0].problems)   # '..' is never followed
    assert samples[1].abs_path is None and any("absolute" in p for p in samples[1].problems)
    assert samples[2].abs_path is None and any("not found" in p for p in samples[2].problems)
    assert samples[3].abs_path is not None and samples[3].abs_path.name == "d1-id-001_digital.png"   # relative to the parent folder
    far = [entry("D1-E", "id_card", "digital", "../../../../etc/passwd", [])]
    assert d1.parse_d1_manifest(synthetic_manifest(tmp_path, far)).samples[0].abs_path is None


def test_unreadable_or_wrong_manifest_is_reported(tmp_path):
    bad = tmp_path / "m.json"
    bad.write_text("{nope", encoding="utf-8")
    assert d1.parse_d1_manifest(bad).problems
    bad.write_text(json.dumps([{"doc_id": "x"}]), encoding="utf-8")
    m = d1.parse_d1_manifest(bad)
    assert m.samples == [] and "files" in m.problems[0]


# ----------------------------------------------------------------------------- one-to-one matching

def test_repeated_fields_match_one_to_one_and_never_reuse_an_extracted_field():
    expected = [exp("txn_amount", "100.00", 0), exp("txn_amount", "200.00", 1), exp("txn_amount", "300.00", 2)]
    extracted = [ext("txn_amount", "300.00", "300.00", bbox=(0.1, 0.5, 0.1, 0.03)),
                 ext("txn_amount", "100.00", "100.00", bbox=(0.1, 0.4, 0.1, 0.03))]
    outcomes, unexpected = d1.match_group(expected, extracted)
    by_value = {o.expected.value: o.result for o in outcomes}
    assert by_value == {"100.00": d1.MATCH, "200.00": d1.MISSING, "300.00": d1.MATCH}
    assert unexpected == []
    used = [id(o.extracted) for o in outcomes if o.extracted is not None]
    assert len(used) == len(set(used)) == 2


def test_two_identical_expected_values_need_two_extracted_fields():
    outcomes, unexpected = d1.match_group([exp("txn_amount", "50.00", 0), exp("txn_amount", "50.00", 1)],
                                          [ext("txn_amount", "50.00", "50.00")])
    assert sorted(o.result for o in outcomes) == [d1.MATCH, d1.MISSING] and unexpected == []


def test_extra_extracted_fields_are_returned_as_unexpected():
    outcomes, unexpected = d1.match_group([exp("dob", "1990-08-12")],
                                          [ext("dob", "12/08/1990", "1990-08-12", bbox=(0.1, 0.1, 0.1, 0.05)),
                                           ext("dob", "01/01/2000", "2000-01-01", bbox=(0.1, 0.5, 0.1, 0.05))])
    assert [o.result for o in outcomes] == [d1.MATCH] and [u.rawValue for u in unexpected] == ["01/01/2000"]


def test_a_different_value_is_a_mismatch_not_a_miss():
    outcomes, unexpected = d1.match_group([exp("dob", "1990-08-12")], [ext("dob", "13/08/1990", "1990-08-13")])
    assert outcomes[0].result == d1.MISMATCH and outcomes[0].extracted.normalizedValue == "1990-08-13" and unexpected == []


def test_matching_is_exact_but_accepts_the_normalized_or_the_raw_text_and_says_which():
    o, _ = d1.match_group([exp("dob", "1990-08-12")], [ext("dob", "12/08/1990", "1990-08-12")])
    assert o[0].result == d1.MATCH and o[0].match_on == "normalizedValue"
    o, _ = d1.match_group([exp("dob", "12/08/1990")], [ext("dob", "12/08/1990", "1990-08-12")])
    assert o[0].result == d1.MATCH and o[0].match_on == "rawValue"
    o, _ = d1.match_group([exp("full_name", "jane sample")], [ext("full_name", "JANE SAMPLE", "JANE SAMPLE")])
    assert o[0].result == d1.MISMATCH                                    # no case folding


def test_matching_is_by_name_and_page():
    s = sample([exp("dob", "1990-08-12", page=2)], doc_type="id_card")
    r = d1.compare_sample(s, {"dob"}, d1.STATUS_OK, [ext("dob", "12/08/1990", "1990-08-12", page=1)])
    results = {o.result for o in r.outcomes}
    assert results == {d1.MISSING, d1.UNEXPECTED}                        # same name on another page does not count
    ok = d1.compare_sample(s, {"dob"}, d1.STATUS_OK, [ext("dob", "12/08/1990", "1990-08-12", page=2)])
    assert [o.result for o in ok.outcomes] == [d1.MATCH]


def test_missing_and_unexpected_fields_are_reported_separately():
    s = sample([exp("dob", "1990-08-12", 0), exp("id_number", "X-1", 1)], doc_type="id_card")
    r = d1.compare_sample(s, {"dob", "id_number", "full_name"}, d1.STATUS_OK,
                          [ext("dob", "12/08/1990", "1990-08-12"), ext("full_name", "A B", "A B")])
    got = {(o.name, o.result) for o in r.outcomes}
    assert got == {("dob", d1.MATCH), ("id_number", d1.MISSING), ("full_name", d1.UNEXPECTED)}
    t = d1.tally([r])
    assert (t.expected, t.matched, t.missing, t.unexpected, t.evaluated) == (2, 1, 1, 1, 2)    # unexpected is not an expected field


def test_bbox_iou_is_informational_and_never_changes_value_accuracy():
    e = exp("dob", "1990-08-12", bbox=(0.1, 0.1, 0.2, 0.1))
    far = ext("dob", "12/08/1990", "1990-08-12", bbox=(0.7, 0.7, 0.2, 0.1))
    same = ext("dob", "12/08/1990", "1990-08-12", bbox=(0.1, 0.1, 0.2, 0.1))
    (a,), _ = d1.match_group([e], [far])
    (b,), _ = d1.match_group([e], [same])
    assert a.result == b.result == d1.MATCH and a.iou == 0.0 and b.iou == 1.0
    (c,), _ = d1.match_group([exp("dob", "1990-08-12")], [same])         # no expected box: no IoU
    assert c.iou is None


def test_iou_value_for_partial_overlap():
    (o,), _ = d1.match_group([exp("dob", "v", bbox=(0.0, 0.0, 0.2, 0.2))], [ext("dob", "v", "v", bbox=(0.1, 0.0, 0.2, 0.2))])
    assert o.iou == pytest.approx(1 / 3, abs=1e-3)


# ----------------------------------------------------------------------------- unsupported, failed, aggregation

def test_fields_without_an_o4_schema_field_are_not_evaluated_and_not_renamed():
    s = sample([exp("statement_total", "3324.70", 0), exp("account_balance", "1.00", 1), exp("full_name", "A", 2)])
    r = d1.compare_sample(s, {"account_number", "total"}, d1.STATUS_OK, [ext("total", "3,324.70", "3324.70")])
    by_name = {o.name: o for o in r.outcomes}
    assert by_name["statement_total"].result == d1.NOT_EVALUATED and "has no field 'statement_total'" in by_name["statement_total"].reason
    assert by_name["full_name"].result == d1.NOT_EVALUATED
    assert by_name["total"].result == d1.UNEXPECTED                       # `total` was NOT used to satisfy `statement_total`
    t = d1.tally([r])
    assert (t.not_evaluated, t.evaluated, t.matched, t.unexpected) == (3, 0, 0, 1)
    assert t.accuracy_evaluated is None and t.accuracy_strict == 0.0


def test_doc_type_without_a_schema_is_entirely_not_evaluated():
    r = d1.compare_sample(sample([exp("a", "1"), exp("b", "2")], doc_type="passport"), None, d1.STATUS_OK, [])
    assert [o.result for o in r.outcomes] == [d1.NOT_EVALUATED] * 2 and "no O4 schema" in r.outcomes[0].reason


def test_supported_fields_of_a_failed_sample_are_failed_not_matched_or_mismatched():
    s = sample([exp("dob", "1990-08-12", 0), exp("nationality", "X", 1)], doc_type="id_card")
    for status in (d1.STATUS_ERROR, d1.STATUS_UNAVAILABLE):
        r = d1.compare_sample(s, {"dob"}, status, [ext("dob", "12/08/1990", "1990-08-12")], ["render failed: boom"])
        assert {o.name: o.result for o in r.outcomes} == {"dob": d1.FAILED, "nationality": d1.NOT_EVALUATED}
        t = d1.tally([r])
        assert (t.matched, t.mismatched, t.missing, t.failed, t.not_evaluated, t.samples_ok, t.unexpected) == (0, 0, 0, 1, 1, 0, 0)
        assert t.accuracy_evaluated is None and t.accuracy_strict == 0.0


def build_results():
    digital = sample([exp("a", "1", 0), exp("b", "2", 1), exp("c", "3", 2), exp("d", "4", 3)], "digital", "id_card", "D1-ID")
    scan = sample([exp("a", "1", 0), exp("b", "2", 1), exp("c", "3", 2), exp("unsupported", "9", 3)], "scan", "id_card", "D1-ID")
    names = {"a", "b", "c", "d"}
    r1 = d1.compare_sample(digital, names, d1.STATUS_OK, [ext("a", "1", "1"), ext("b", "2", "2"), ext("c", "3", "3"), ext("d", "x", "x")])
    r2 = d1.compare_sample(scan, names, d1.STATUS_OK, [ext("a", "1", "1"), ext("b", "bad", "bad"), ext("zzz", "q", "q")])
    return [r1, r2]


def test_digital_scan_and_overall_aggregation():
    r1, r2 = build_results()
    dig, scn = d1.by_variant([r1, r2]).items()
    assert dig[0] == "digital" and scn[0] == "scan"
    d, s = dig[1], scn[1]
    assert (d.expected, d.evaluated, d.matched, d.mismatched, d.missing, d.not_evaluated, d.failed) == (4, 4, 3, 1, 0, 0, 0)
    assert (s.expected, s.evaluated, s.matched, s.mismatched, s.missing, s.not_evaluated, s.unexpected) == (4, 3, 1, 1, 1, 1, 1)
    assert d.accuracy_evaluated == 0.75 and s.accuracy_evaluated == pytest.approx(1 / 3) and s.accuracy_strict == 0.25
    o = d1.tally([r1, r2])
    assert (o.samples, o.samples_ok, o.expected, o.evaluated, o.matched, o.mismatched, o.missing, o.not_evaluated, o.unexpected) == \
           (2, 2, 8, 7, 4, 2, 1, 1, 1)
    assert o.accuracy_evaluated == pytest.approx(4 / 7) and o.accuracy_strict == 0.5


def test_report_states_denominators_and_never_claims_full_accuracy_with_unevaluated_fields():
    r1, r2 = build_results()
    manifest = d1.D1Manifest(Path("m.json"))
    text = d1.format_report(manifest, [r1, r2])
    for column in ("Samples ok", "Expected", "Evaluated", "Matched", "Mismatched", "Missing", "Not evaluated", "Failed",
                   "Acc % (of evaluated)", "Acc % (of all expected)", "OVERALL", "digital", "scan"):
        assert column in text
    assert "COVERAGE IS INCOMPLETE" in text and "NOT EVALUATED" in text and "UNEXPECTED" in text
    only_unsupported = d1.compare_sample(sample([exp("zzz", "1")]), set(), d1.STATUS_OK, [])
    text2 = d1.format_report(manifest, [only_unsupported])
    assert "100.0" not in text2 and "n/a" in text2 and "COVERAGE IS INCOMPLETE" in text2
    perfect = d1.compare_sample(sample([exp("a", "1")]), {"a"}, d1.STATUS_OK, [ext("a", "1", "1")])
    text3 = d1.format_report(manifest, [perfect])
    assert "100.0" in text3 and "COVERAGE IS INCOMPLETE" not in text3


def test_json_export_carries_the_same_counts():
    r1, r2 = build_results()
    data = d1.to_jsonable(d1.D1Manifest(Path("m.json")), [r1, r2])
    assert data["overall"]["matched"] == 4 and data["overall"]["notEvaluated"] == 1
    assert set(data["byVariant"]) == {"digital", "scan"} and data["samples"][1]["variant"] == "scan"
    json.dumps(data)


# ----------------------------------------------------------------------------- independence of samples

def test_each_sample_is_processed_independently_and_one_crash_does_not_hide_the_others(tmp_path):
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path))
    seen = []

    def runner(smp, workdir, dpi, timeout, registry):
        seen.append(smp.runtime_id)
        if smp.runtime_id == "D1-ID-001__scan":
            raise RuntimeError("boom")
        fields = [ext("dob", "12/08/1990", "1990-08-12")] if smp.doc_type == "id_card" else []
        return d1.PipelineRun(d1.STATUS_OK, [], fields, smp.doc_type, "fake")

    results = d1.evaluate(manifest, tmp_path / "work", runner=runner)
    assert seen == [s.runtime_id for s in manifest.samples] and len(results) == 6
    by_id = {r.sample.runtime_id: r for r in results}
    assert by_id["D1-ID-001__scan"].status == d1.STATUS_ERROR and "evaluator error" in by_id["D1-ID-001__scan"].reasons[0]
    assert by_id["D1-ID-001__digital"].status == d1.STATUS_OK
    assert [o.result for o in by_id["D1-ID-001__digital"].outcomes] == [d1.MATCH]
    assert [o.result for o in by_id["D1-ID-001__scan"].outcomes] == [d1.FAILED]      # the digital result did not leak into the scan
    t = d1.tally(results)
    assert t.samples == 6 and t.samples_ok == 5


def test_bank_statement_names_that_o4_does_not_extract_are_not_evaluated(tmp_path):
    manifest = d1.parse_d1_manifest(synthetic_manifest(tmp_path))
    bank = next(s for s in manifest.samples if s.doc_type == "bank_statement")
    names = d1.supported_fields("bank_statement")
    assert "statement_total" not in names and "txn_amount" not in names and "total" in names
    r = d1.compare_sample(bank, names, d1.STATUS_OK, [])
    by = {}
    for o in r.outcomes:
        by.setdefault(o.name, []).append(o.result)
    assert by["txn_amount"] == [d1.NOT_EVALUATED] * 3 and by["statement_total"] == [d1.NOT_EVALUATED]
    assert by["full_name"] == [d1.NOT_EVALUATED]                           # the schema calls it account_holder: not renamed


# ----------------------------------------------------------------------------- the real pipeline, end to end (generated documents)

@pytest.fixture
def fake_ocr(monkeypatch):
    page = X.id_card()
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setattr(ocr, "resolve_engine", lambda: ocr_core.EngineStatus(True, cmd="tesseract", version="5.5.3"))
    monkeypatch.setattr(ocr_core.pytesseract, "image_to_data", lambda image, **kw: tsv_from_page(page, *image.size))
    monkeypatch.setattr(ocr_core.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    return page


def pipeline_manifest(tmp_path):
    folder = tmp_path / "samples"
    (folder / "d1-lite").mkdir(parents=True)
    (folder / "d1-lite" / "id_digital.png").write_bytes(make_png_bytes((1011, 638)))
    (folder / "d1-lite" / "id_scan.png").write_bytes(b"this is not an image")
    ok_fields = [f("full_name", "JANE ALICE SAMPLE", (0.1, 0.2, 0.3, 0.05)), f("dob", "1990-08-12"),
                 f("id_number", "000-FICTIONAL-01"), f("nationality", "EXAMPLELANDER")]
    files = [entry("D1-ID-001", "id_card", "digital", "d1-lite/id_digital.png", ok_fields),
             entry("D1-ID-001", "id_card", "scan", "d1-lite/id_scan.png", ok_fields),
             entry("D1-ID-002", "id_card", "digital", "d1-lite/not_there.png", ok_fields)]
    path = folder / "manifest.json"
    path.write_text(json.dumps({"files": files}), encoding="utf-8")
    return path


def test_real_pipeline_runs_each_sample_and_isolates_failures(tmp_path, fake_ocr):
    path = pipeline_manifest(tmp_path)
    source = sha(path.parent / "d1-lite" / "id_digital.png")
    results = d1.evaluate(d1.parse_d1_manifest(path), tmp_path / "work", dpi=100, timeout=60)
    by_id = {r.sample.runtime_id: r for r in results}
    ok = by_id["D1-ID-001__digital"]
    assert ok.status == d1.STATUS_OK and ok.classified_as == "id_card" and ok.engines == "tesseract"
    res = {o.name: o.result for o in ok.outcomes if o.result != d1.UNEXPECTED}
    assert res == {"full_name": d1.MATCH, "dob": d1.MATCH, "id_number": d1.MATCH, "nationality": d1.NOT_EVALUATED}
    unexpected = [o for o in ok.outcomes if o.result == d1.UNEXPECTED]
    assert [o.name for o in unexpected] == ["address_permanent"]               # found on the page, not in the manifest
    assert any(o.iou is not None for o in ok.outcomes if o.name == "full_name")    # a box was compared (informational)
    bad = by_id["D1-ID-001__scan"]
    assert bad.status == d1.STATUS_ERROR and "render failed" in bad.reasons[0]
    assert {o.result for o in bad.outcomes} == {d1.FAILED, d1.NOT_EVALUATED}
    missing = by_id["D1-ID-002__digital"]
    assert missing.status == d1.STATUS_ERROR and "not found" in missing.reasons[0]
    t = d1.tally(results)
    assert t.samples == 3 and t.samples_ok == 1 and t.matched == 3 and t.failed == 6 and t.unexpected == 1
    assert sha(path.parent / "d1-lite" / "id_digital.png") == source             # the source file is never modified


def test_ocr_unavailable_is_reported_as_unavailable_not_as_a_comparison(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "tesseract")
    monkeypatch.setenv("TESSERACT_CMD", "C:\\nope\\tesseract.exe")
    path = pipeline_manifest(tmp_path)
    results = d1.evaluate(d1.parse_d1_manifest(path), tmp_path / "work", dpi=100, timeout=60)
    first = results[0]
    assert first.status == d1.STATUS_UNAVAILABLE and any("ocr" in r for r in first.reasons)
    assert d1.tally([first]).matched == 0 and {o.result for o in first.outcomes} == {d1.FAILED, d1.NOT_EVALUATED}


def test_c1_fixtures_are_never_used(tmp_path, fake_ocr, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the D1 evaluator must not touch C1 fixtures")

    monkeypatch.setattr("app.ocr_fixtures.fixtures_dir", boom)
    monkeypatch.setattr("app.ocr_fixtures.list_fixtures", boom)
    monkeypatch.setattr("app.ocr_fixtures.seed_ocr", boom)
    d1.evaluate(d1.parse_d1_manifest(pipeline_manifest(tmp_path)), tmp_path / "work", dpi=100, timeout=60)


# ----------------------------------------------------------------------------- command line

def load_cli():
    spec = importlib.util.spec_from_file_location("d1_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_without_a_manifest_runs_nothing_and_exits_2(tmp_path, capsys):
    code = load_cli().main(["--manifest", str(tmp_path / "missing.json")])
    out = capsys.readouterr().out
    assert code == 2 and "NOT COMPUTED" in out and "Nothing was run" in out


def test_cli_reports_failures_exit_codes_and_json(tmp_path, capsys, fake_ocr):
    path = pipeline_manifest(tmp_path)
    before = sha(path)
    out_json = tmp_path / "report.json"
    code = load_cli().main(["--manifest", str(path), "--dpi", "100", "--json", str(out_json)])
    out = capsys.readouterr().out
    assert code == 3                                                      # two samples failed
    assert "SAMPLES PROCESSED SUCCESSFULLY: 1 of 3" in out and "COVERAGE IS INCOMPLETE" in out
    assert "D1-ID-001__digital" in out and "D1-ID-001__scan" in out       # both variants are reported separately
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["overall"]["samples"] == 3 and data["overall"]["samplesOk"] == 1 and data["byVariant"]["digital"]["samples"] == 2
    assert sha(path) == before


def test_current_manifest_shape_documents_file_region(tmp_path):
    """The repo's manifest lists entries under 'documents', with 'file' paths and the contract box in 'region'."""
    from app import d1_eval
    from app.extract_report import is_d1_manifest
    (tmp_path / "genuine").mkdir()
    (tmp_path / "genuine" / "a.png").write_bytes(b"x")
    manifest = {"documents": [{"doc_id": "id_card_001", "doc_type": "id_card", "variant": "original", "file": "genuine/a.png",
                               "fields": [{"name": "full_name", "value": "Bradley Evans", "bbox": [0.1, 0.2, 0.5, 0.3],
                                           "region": {"bbox": [0.1, 0.2, 0.4, 0.1]}}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert is_d1_manifest(path)
    parsed = d1_eval.parse_d1_manifest(path)
    assert parsed.problems == [] and len(parsed.samples) == 1
    sample = parsed.samples[0]
    assert sample.abs_path is not None and sample.fields[0].bbox == (0.1, 0.2, 0.4, 0.1) and sample.fields[0].bbox_warning is None
