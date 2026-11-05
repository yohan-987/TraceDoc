import pytest

from app.config import Settings
from app.models import InvalidDocIdError
from app.render import render_document

BAD_IDS = ["../secret", "..\\secret", "a/b", "a b", "C:\\x", "/etc/passwd", "doc.pdf", "", "a\n", "id;rm"]


@pytest.mark.parametrize("doc_id", BAD_IDS)
def test_api_rejects_invalid_doc_id(client, doc_id):
    r = client.post("/analyze", json={"docId": doc_id})
    assert r.status_code == 422


def test_api_rejects_path_fields(client, put_original):
    r = client.post("/analyze", json={"docId": "CASE-001-DOC-01", "path": "/etc/passwd"})
    assert r.status_code == 422


def test_api_rejects_missing_doc_id(client):
    assert client.post("/analyze", json={}).status_code == 422


@pytest.mark.parametrize("doc_id", BAD_IDS)
def test_renderer_also_validates(tmp_path, doc_id):
    with pytest.raises(InvalidDocIdError):
        render_document(doc_id, Settings(data_dir=tmp_path))
