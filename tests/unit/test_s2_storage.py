"""S2: FileStore (local disk) and the template attachment routes using it through a provider."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters import providers
from app.adapters.storage import LocalFileStore, UnsafeStorageKey
from app.core.auth import get_current_user
from app.core.errors import register_domain_error_handler
from app.routers.template_router import router as template_api
from app.routers.template_router import template_service
from app.services.campaign_template_service import EmailTemplateService


# ── LocalFileStore ─────────────────────────────────────────────

def test_local_store_round_trip(tmp_path):
    store = LocalFileStore(tmp_path / "attachments")
    assert not store.exists("a_report.pdf")
    store.put("a_report.pdf", b"%PDF-1")
    assert store.exists("a_report.pdf")
    assert store.get("a_report.pdf") == b"%PDF-1"
    assert (tmp_path / "attachments" / "a_report.pdf").read_bytes() == b"%PDF-1"  # same place as before
    assert store.local_path("a_report.pdf") == tmp_path / "attachments" / "a_report.pdf"
    assert store.url("a_report.pdf").startswith("file://") and store.url("a_report.pdf").endswith("/a_report.pdf")
    store.delete("a_report.pdf")
    assert not store.exists("a_report.pdf")
    store.delete("a_report.pdf")  # missing: not an error
    with pytest.raises(FileNotFoundError):
        store.get("a_report.pdf")


@pytest.mark.parametrize("key", ["", ".", "..", "../etc/passwd", "sub/file", "..\\x", "a\x00b"])
def test_local_store_keys_stay_inside_the_root(tmp_path, key):
    store = LocalFileStore(tmp_path)
    with pytest.raises(UnsafeStorageKey):
        store.put(key, b"x")
    assert not store.exists(key)
    assert store.local_path(key) is None
    with pytest.raises(FileNotFoundError):
        store.get(key)
    store.delete(key)  # no-op


# ── template attachment routes ─────────────────────────────────

class MemoryStore:
    """A remote-style store: no local path, so downloads are sent as bytes."""

    def __init__(self):
        self.files = {}

    def put(self, key, data):
        self.files[key] = data

    def get(self, key):
        return self.files[key]

    def delete(self, key):
        self.files.pop(key, None)

    def exists(self, key):
        return key in self.files

    def url(self, key):
        return f"memory://{key}"

    def local_path(self, key):
        return None


ME = SimpleNamespace(user_id="u1", tenant_id="t1", role="SUPER_ADMIN", custom_permissions=None)


class FakeContent:
    """The template's slice of app/repositories/campaigns_content.py (A2 moved the SQL there)."""

    def __init__(self, attachments=(), shared=False):
        self.rows = {a.attachment_id: a for a in attachments}
        self.shared = shared
        self.added, self.deleted = [], []

    def template_for_tenant(self, tenant_id, template_id):
        return SimpleNamespace(template_id=template_id) if tenant_id == "t1" else None

    def attachment_count(self, template_id):
        return len(self.rows)

    def attachment(self, template_id, attachment_id):
        return self.rows.get(attachment_id)

    def storage_shared(self, storage_path, attachment_id):
        return self.shared

    def add(self, entity):
        self.added.append(entity)

    def delete(self, entity):
        self.deleted.append(entity)

    def refresh(self, entity):
        entity.created_at = datetime(2026, 10, 1)


class FakeUow:
    def __init__(self, content):
        self.content, self.commits = content, 0
        self.session = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def commit(self):
        self.commits += 1


def _client(content, store):
    uow = FakeUow(content)
    app = FastAPI()
    app.include_router(template_api)
    register_domain_error_handler(app)
    app.dependency_overrides[get_current_user] = lambda: ME
    app.dependency_overrides[template_service] = lambda: EmailTemplateService(uow=uow)
    app.dependency_overrides[providers.attachment_store] = lambda: store
    return TestClient(app), uow


def _attachment(**kw):
    base = dict(attachment_id="att1", template_id="tpl1", filename="report.pdf", content_type="application/pdf",
                size_bytes=6, storage_path="k_report.pdf", created_at=datetime(2026, 10, 1))
    base.update(kw)
    return SimpleNamespace(**base)


def test_upload_writes_through_the_store():
    content, store = FakeContent(), MemoryStore()
    client, uow = _client(content, store)
    r = client.post("/templates/tpl1/attachments", files={"file": ("report.pdf", b"%PDF-1", "application/pdf")})
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "report.pdf" and r.json()["size_bytes"] == 6
    [(key, data)] = store.files.items()
    assert key.endswith("_report.pdf") and data == b"%PDF-1"
    assert content.added[0].storage_path == key
    assert uow.commits == 1


def test_download_from_a_remote_store_sends_the_bytes():
    store = MemoryStore()
    store.put("k_report.pdf", b"%PDF-1")
    client, _ = _client(FakeContent([_attachment()]), store)
    r = client.get("/templates/tpl1/attachments/att1/download")
    assert r.status_code == 200 and r.content == b"%PDF-1"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="report.pdf"'


def test_download_from_local_disk_streams_the_file(tmp_path):
    store = LocalFileStore(tmp_path)
    store.put("k_report.pdf", b"%PDF-1")
    client, _ = _client(FakeContent([_attachment(filename="Résumé.pdf")]), store)
    r = client.get("/templates/tpl1/attachments/att1/download")
    assert r.status_code == 200 and r.content == b"%PDF-1"
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''R%C3%A9sum%C3%A9.pdf"


def test_download_of_a_missing_file_is_404():
    client, _ = _client(FakeContent([_attachment()]), MemoryStore())
    r = client.get("/templates/tpl1/attachments/att1/download")
    assert r.status_code == 404 and r.json()["detail"] == "Attachment file missing from storage"


def test_delete_removes_the_file_unless_shared():
    store = MemoryStore()
    store.put("k_report.pdf", b"%PDF-1")
    client, _ = _client(FakeContent([_attachment()]), store)
    assert client.delete("/templates/tpl1/attachments/att1").status_code == 204
    assert store.files == {}

    store.put("k_report.pdf", b"%PDF-1")
    client, _ = _client(FakeContent([_attachment()], shared=True), store)
    assert client.delete("/templates/tpl1/attachments/att1").status_code == 204
    assert store.files == {"k_report.pdf": b"%PDF-1"}  # a duplicated campaign still uses it
