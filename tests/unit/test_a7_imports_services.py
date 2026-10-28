"""A7 ImportService with a fake repository, unit of work and file store (no database)."""
import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.imports import ImportLimits
from app.models.crm import ImportJob
from app.services.import_service import ImportService, UploadedFile, errors_csv, job_dict

ADMIN = SimpleNamespace(user_id="u1", tenant_id="t1", role="SUPER_ADMIN", sales_level=None, custom_permissions=None,
                        email="admin@acme.example", first_name="Ada", last_name="Admin")
AGENT = SimpleNamespace(user_id="u2", tenant_id="t1", role="AGENT", sales_level=None, custom_permissions=[],
                        email="agent@acme.example", first_name="Al", last_name="Agent")


class FakeStore:
    def __init__(self):
        self.files = {}

    def put(self, key, data):
        self.files[key] = data

    def get(self, key):
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def delete(self, key):
        self.files.pop(key, None)

    def exists(self, key):
        return key in self.files


class FakeRepo:
    def __init__(self):
        self.jobs = {}
        self.added = []
        self.contacts = {}
        self.lists = {}
        self.saved = {}
        self.deleted = []
        self.defs = []

    # reference data
    def field_definitions(self, tenant_id):
        return list(self.defs)

    def users(self, tenant_id):
        return [ADMIN, SimpleNamespace(user_id="u3", tenant_id="t1", email="Owner@Acme.example", first_name="Olga",
                                       last_name="Owner")]

    def user(self, tenant_id, user_id):
        return next((u for u in self.users(tenant_id) if u.user_id == user_id), None)

    def user_names(self, ids):
        return {i: "Ada Admin" for i in ids}

    def unsubscribed_emails(self, tenant_id):
        return set()

    def static_list(self, tenant_id, list_id):
        return self.lists.get(list_id)

    def contacts_by_email(self, tenant_id, emails):
        return {e.lower(): self.contacts[e.lower()] for e in emails if e.lower() in self.contacts}

    def live_account(self, tenant_id, domain=None, name=None):
        return None

    # writes
    def add(self, entity):
        self.added.append(entity)
        if isinstance(entity, ImportJob):
            entity.import_id = entity.import_id or str(uuid.uuid4())
            self.jobs[entity.import_id] = entity
        if hasattr(entity, "list_id") and type(entity).__name__ == "ProspectList":
            entity.list_id = entity.list_id or "L-new"
        if type(entity).__name__ == "ImportMapping":
            entity.mapping_id = entity.mapping_id or str(uuid.uuid4())
            self.saved[entity.mapping_id] = entity
        return entity

    def flush(self):
        pass

    def free_list_name(self, tenant_id, name):
        return name

    def delete(self, entity):
        self.deleted.append(entity)
        self.saved.pop(getattr(entity, "mapping_id", None), None)

    # jobs
    def job(self, tenant_id, import_id):
        job = self.jobs.get(import_id)
        return job if job is not None and job.tenant_id == tenant_id else None

    def job_by_id(self, import_id):
        return self.jobs.get(import_id)

    def history(self, tenant_id, offset, limit):
        rows = [j for j in self.jobs.values() if j.tenant_id == tenant_id]
        return rows[offset:offset + limit], len(rows)

    # mappings
    def mappings(self, tenant_id):
        return [m for m in self.saved.values() if m.tenant_id == tenant_id]

    def mapping(self, tenant_id, mapping_id):
        m = self.saved.get(mapping_id)
        return m if m is not None and m.tenant_id == tenant_id else None

    def mapping_named(self, tenant_id, name):
        return next((m for m in self.saved.values() if m.tenant_id == tenant_id and m.name == name), None)


class FakeUow:
    def __init__(self):
        self.imports = FakeRepo()
        self.session = None
        self.enters = self.commits = self.rollbacks = 0

    def __enter__(self):
        self.enters += 1
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def is_retryable(self, exc):
        return False


@pytest.fixture
def svc():
    return ImportService(FakeUow(), store=FakeStore(), limits=ImportLimits(max_rows=100, sync_max_rows=3))


CSV = ("Email,Name,Owner,Seats\n"
       "new@acme.example,\"Doe, Jane\",Olga Owner,\"1,200\"\n"
       "Old <old@acme.example>,Old Timer,,\n"
       "not-an-email,X,,\n"
       "new@acme.example,Dup,,\n").encode()
MAPPING = {"Email": "contact.email", "Name": "contact.full_name", "Owner": "contact.owner_email"}


def _upload(svc, data=CSV, name="people.csv"):
    return svc.upload(ADMIN, UploadedFile(name, data))


def test_preview_suggests_and_needs_manage_permission(svc):
    body = svc.preview(ADMIN, UploadedFile("p.csv", CSV))
    assert body["row_count"] == 4 and body["suggested_mapping"]["Email"] == "contact.email"
    assert body["suggested_mapping"]["Owner"] == "contact.owner_email" and body["sheet_names"] == []
    with pytest.raises(PermissionDenied):
        svc.preview(AGENT, UploadedFile("p.csv", CSV))


def test_preview_refuses_unreadable_file(svc):
    with pytest.raises(BadRequestError, match="not a valid .xlsx"):
        svc.preview(ADMIN, UploadedFile("p.xlsx", b"nope"))


def test_upload_stores_a_tenant_scoped_file(svc):
    body = _upload(svc)
    job = svc.uow.imports.jobs[body["import"]["import_id"]]
    assert job.status == "UPLOADED" and job.total_rows == 4 and job.file_size == len(CSV)
    assert job.file_path == f"t1_{job.import_id}.csv" and svc.store.files[job.file_path] == CSV
    assert svc.uow.commits == 1 and body["import"]["background"] is True


def test_dry_run_counts_without_writing(svc):
    job_id = _upload(svc)["import"]["import_id"]
    svc.uow.imports.contacts["old@acme.example"] = SimpleNamespace(email="old@acme.example", deleted_at=None,
                                                                   owner_id="u1", prospect_id="p-old")
    added_before = len(svc.uow.imports.added)
    body = svc.dry_run(ADMIN, job_id, MAPPING, {})
    assert (body["create"], body["update"], body["skip"], body["fail"]) == (1, 1, 0, 2)
    reasons = [e["reason"] for e in body["errors"]]
    assert reasons == ["Invalid email 'not-an-email'", "Duplicate of row 2 in this file"]
    assert len(svc.uow.imports.added) == added_before and svc.uow.rollbacks >= 1

    body = svc.dry_run(ADMIN, job_id, MAPPING, {"mode": "CREATE"})
    assert (body["create"], body["update"], body["skip"]) == (1, 0, 1)
    body = svc.dry_run(ADMIN, job_id, MAPPING, {"mode": "UPDATE_ONLY"})
    assert (body["create"], body["update"], body["skip"]) == (0, 1, 1)


def test_dry_run_reports_unknown_owner_and_bad_mapping(svc):
    data = "Email,Owner\na@acme.example,nobody@acme.example\n".encode()
    job_id = _upload(svc, data)["import"]["import_id"]
    body = svc.dry_run(ADMIN, job_id, {"Email": "contact.email", "Owner": "contact.owner_email"}, {})
    assert body["fail"] == 1 and "is not a user in this workspace" in body["errors"][0]["reason"]
    with pytest.raises(BadRequestError, match="Unknown target"):
        svc.dry_run(ADMIN, job_id, {"Email": "contact.nope"}, {})
    with pytest.raises(BadRequestError, match="Map an Email column"):
        svc.dry_run(ADMIN, job_id, {"Owner": "skip"}, {})
    with pytest.raises(BadRequestError, match="mode must be one of"):
        svc.dry_run(ADMIN, job_id, {"Email": "contact.email"}, {"mode": "MERGE"})


def test_dry_run_company_only_file(svc):
    data = "Company,Domain\nAcme,acme.example\n,not a domain\n".encode()
    job_id = _upload(svc, data)["import"]["import_id"]
    body = svc.dry_run(ADMIN, job_id, {"Company": "company.name", "Domain": "company.domain"}, {})
    assert body["companies_create"] == 1 and body["fail"] == 1


def test_start_queues_once_with_a_new_list(svc):
    job_id = _upload(svc)["import"]["import_id"]
    body = svc.start(ADMIN, job_id, MAPPING, {"new_list_name": "Expo leads", "mode": "CREATE"})
    job = svc.uow.imports.jobs[job_id]
    assert body["status"] == "QUEUED" and job.queued_at is not None and job.mode == "CREATE"
    assert job.mapping == MAPPING and job.options["list_id"] == "L-new" and job.options["list_name"] == "Expo leads"
    with pytest.raises(ConflictError):
        svc.start(ADMIN, job_id, MAPPING, {})


def test_start_with_unknown_static_list_is_refused(svc):
    job_id = _upload(svc)["import"]["import_id"]
    with pytest.raises(BadRequestError, match="Static list not found"):
        svc.start(ADMIN, job_id, MAPPING, {"list_id": "missing"})
    with pytest.raises(BadRequestError, match="Owner must be a user"):
        svc.start(ADMIN, job_id, MAPPING, {"owner_id": "ghost"})


def test_cancel_and_resume(svc):
    job_id = _upload(svc)["import"]["import_id"]
    job = svc.uow.imports.jobs[job_id]
    key = job.file_path
    assert svc.cancel(ADMIN, job_id)["status"] == "CANCELLED" and key not in svc.store.files
    with pytest.raises(ConflictError):
        svc.cancel(ADMIN, job_id)

    job_id = _upload(svc)["import"]["import_id"]
    job = svc.uow.imports.jobs[job_id]
    job.status = "RUNNING"
    body = svc.cancel(ADMIN, job_id)
    assert body["status"] == "RUNNING" and body["cancel_requested"] is True

    job.status, job.mapping, job.cancel_requested = "FAILED", MAPPING, True
    body = svc.resume(ADMIN, job_id)
    assert body["status"] == "QUEUED" and job.cancel_requested is False
    with pytest.raises(ConflictError):
        svc.resume(ADMIN, job_id)


def test_jobs_of_other_workspaces_are_not_found(svc):
    job_id = _upload(svc)["import"]["import_id"]
    other = SimpleNamespace(**{**vars(ADMIN), "tenant_id": "t2"})
    for call in (lambda: svc.detail(other, job_id), lambda: svc.cancel(other, job_id),
                 lambda: svc.dry_run(other, job_id, MAPPING, {}), lambda: svc.error_report(other, job_id)):
        with pytest.raises(NotFoundError):
            call()


def test_sync_import_of_a_large_file_is_queued(svc):
    status, body = svc.run_sync(ADMIN, UploadedFile("big.csv", CSV), json.dumps(MAPPING), "{}")
    assert status == 202 and body["status"] == "QUEUED" and body["errors"] == []
    with pytest.raises(BadRequestError, match="must be JSON"):
        svc.run_sync(ADMIN, UploadedFile("big.csv", CSV), "{oops", "{}")


def test_saved_mappings(svc):
    first = svc.save_mapping(ADMIN, "Trade show", MAPPING, {"date_order": "DMY"})
    again = svc.save_mapping(ADMIN, "Trade show", {"Email": "contact.email"}, None)
    assert first["mapping_id"] == again["mapping_id"] and again["mapping"] == {"Email": "contact.email"}
    assert [m["name"] for m in svc.mappings(ADMIN)] == ["Trade show"]
    with pytest.raises(BadRequestError):
        svc.save_mapping(ADMIN, "", MAPPING, None)
    assert svc.delete_mapping(ADMIN, first["mapping_id"])["status"] == "deleted"
    with pytest.raises(NotFoundError):
        svc.delete_mapping(ADMIN, first["mapping_id"])


def test_history_and_error_report_are_formula_guarded(svc):
    job_id = _upload(svc)["import"]["import_id"]
    job = svc.uow.imports.jobs[job_id]
    job.errors = [{"row": 2, "reason": "Invalid email '=cmd'", "values": {"Email": "=HYPERLINK(1)"}}]
    name, data = errors_csv(job)
    text = data.decode("utf-8-sig")
    assert name == "people-errors.csv" and "'=HYPERLINK(1)" in text
    page = svc.history(ADMIN, 1, 25)
    assert page["total"] == 1 and page["items"][0]["created_by_name"] == "Ada Admin"
    assert job_dict(job)["progress"] == 0
