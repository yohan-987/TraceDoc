"""Schemas in app/schemas/*.yaml: content, validation and failure handling."""
import shutil

import pytest

from app import extract_schema as s

REQUIRED = {
    "id_card": ["full_name", "dob", "id_number", "address_permanent"],
    "application_form": ["full_name", "id_number", "dob", "address_permanent", "address_current", "requested_amount", "application_date", "signature"],
    "bank_statement": ["account_holder", "account_number", "opening_balance", "closing_balance", "total"],
}


@pytest.fixture
def schema_dir(tmp_path):
    for f in s.SCHEMAS_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


@pytest.mark.parametrize("doc_type", list(REQUIRED))
def test_shipped_schemas_define_the_required_fields(doc_type):
    schema = s.load_schema(doc_type)
    assert [f.name for f in schema.fields] == REQUIRED[doc_type]
    for f in schema.fields:
        assert f.semanticType and f.strategies and all(0 <= st.confidence <= 1 for st in f.strategies)


def test_shipped_settings_load():
    st = s.load_settings()
    assert 0 < st.lowOcrConfidence < 1 and st.dateOrder in ("DMY", "MDY", None) and "regionMissPenalty" in st.scoring


def test_unsupported_doc_types_have_no_schema():
    for doc_type in ("other", "UNKNOWN", "AMBIGUOUS", "passport"):
        with pytest.raises(s.SchemaError):
            s.load_schema(doc_type)


def test_missing_schema_file(schema_dir):
    (schema_dir / "id_card.yaml").unlink()
    with pytest.raises(s.SchemaError) as info:
        s.load_schema("id_card", schema_dir)
    assert "not found" in str(info.value)


def test_missing_settings_file(schema_dir):
    (schema_dir / "_settings.yaml").unlink()
    with pytest.raises(s.SchemaError):
        s.load_settings(schema_dir)


@pytest.mark.parametrize("breakage,fragment", [
    ("fields: [", "unreadable"),                                      # malformed YAML
    ("docType: id_card\nschemaVersion: '1'\nfields: []\n", "non-empty"),
    ("docType: bank_statement\nschemaVersion: '1'\nfields: [{name: a}]\n", "docType must be"),
    ("- just\n- a list\n", "mapping"),
])
def test_malformed_schema_files(schema_dir, breakage, fragment):
    (schema_dir / "id_card.yaml").write_text(breakage, encoding="utf-8")
    with pytest.raises(s.SchemaError) as info:
        s.load_schema("id_card", schema_dir)
    assert fragment in str(info.value)


@pytest.mark.parametrize("old,new,fragment", [
    ("kind: anchor\n        anchors: [\"date of birth\"", "kind: telepathy\n        anchors: [\"date of birth\"", "kind"),
    ('anchors: ["date of birth", "birth date", "dob", "born"]', 'anchors: ["(unclosed"]', "regex"),
    ("normalizer: date", "normalizer: wizardry", "normalizer"),
    ("validators: [date]", "validators: [clairvoyance]", "validators"),
    ("confidence: 0.92\n\n  - name: id_number", "confidence: 1.7\n\n  - name: id_number", "confidence"),
    ("name: dob", "name: Not-Snake", "snake_case"),
])
def test_invalid_field_definitions_are_rejected(schema_dir, old, new, fragment):
    path = schema_dir / "id_card.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(s.SchemaError) as info:
        s.load_schema("id_card", schema_dir)
    assert fragment in str(info.value)


def test_duplicate_field_names_are_rejected(schema_dir):
    path = schema_dir / "id_card.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("name: dob", "name: full_name", 1), encoding="utf-8")
    with pytest.raises(s.SchemaError) as info:
        s.load_schema("id_card", schema_dir)
    assert "duplicate" in str(info.value)


def test_schema_directory_can_be_overridden(schema_dir, monkeypatch):
    monkeypatch.setenv("FIELD_SCHEMAS_DIR", str(schema_dir))
    assert s.schemas_dir() == schema_dir
    assert s.load_schema("id_card").docType == "id_card"
