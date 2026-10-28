"""A7 import repository against MySQL: the worker's claim, stale detection, interrupted sync jobs, tenant scope."""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.models.crm import ImportJob
from app.models.import_mapping import ImportMapping
from app.repositories.imports import SqlAlchemyImportRepository
from tests.conftest import RUN, SessionLocal, register_tenant, sql


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "airp")


def _user(ws):
    return sql("SELECT user_id FROM users WHERE email = :e", e=ws["email"])


def _job(ws, **kw):
    with SessionLocal() as db:
        job = ImportJob(tenant_id=ws["tenant_id"], created_by=_user(ws), file_name="x.csv", **kw)
        db.add(job)
        db.commit()
        return job.import_id


def _clear_queue():
    sql("UPDATE import_jobs SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'RUNNING') AND file_path IS NOT NULL")


def test_claim_takes_queued_then_stale_and_never_a_live_one(ws):
    _clear_queue()
    now = utcnow()
    live = _job(ws, status="RUNNING", file_path="k1", heartbeat_at=now)
    queued = _job(ws, status="QUEUED", file_path="k2", queued_at=now - timedelta(minutes=1))
    stale = _job(ws, status="RUNNING", file_path="k3", heartbeat_at=now - timedelta(minutes=30), queued_at=now)
    sync_running = _job(ws, status="RUNNING")  # synchronous import: never the worker's
    with SessionLocal() as db:
        repo = SqlAlchemyImportRepository(db)
        cutoff = now - timedelta(minutes=2)
        first = repo.claim_next(cutoff, now, "w1")
        second = repo.claim_next(cutoff, now, "w1")
        third = repo.claim_next(cutoff, now, "w1")
        db.commit()
    assert (first, second, third) == (queued, stale, None)
    assert sql("SELECT CONCAT(status, '/', claimed_by, '/', attempts) FROM import_jobs WHERE import_id = :i",
               i=queued) == "RUNNING/w1/1"
    assert sql("SELECT claimed_by FROM import_jobs WHERE import_id = :i", i=live) is None
    assert sql("SELECT status FROM import_jobs WHERE import_id = :i", i=sync_running) == "RUNNING"
    _clear_queue()


def test_interrupted_sync_jobs_are_failed(ws):
    old = _job(ws, status="RUNNING")
    sql("UPDATE import_jobs SET started_at = UTC_TIMESTAMP() - INTERVAL 2 HOUR WHERE import_id = :i", i=old)
    recent = _job(ws, status="RUNNING")
    with SessionLocal() as db:
        n = SqlAlchemyImportRepository(db).fail_interrupted(utcnow() - timedelta(hours=1), utcnow(), "interrupted")
        db.commit()
    assert n >= 1
    assert sql("SELECT status FROM import_jobs WHERE import_id = :i", i=old) == "FAILED"
    assert sql("SELECT status FROM import_jobs WHERE import_id = :i", i=recent) == "RUNNING"
    sql("UPDATE import_jobs SET status = 'FAILED' WHERE import_id = :i", i=recent)


def test_jobs_mappings_and_contacts_are_tenant_scoped(client, ws):
    other = register_tenant(client, "airq")
    job_id = _job(ws, status="COMPLETED")
    email = f"Scope.{uuid.uuid4().hex[:6]}@airp{RUN}.example.com"
    r = client.post("/contacts", headers=ws["headers"], json={"email": email, "first_name": "Scope"})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        repo = SqlAlchemyImportRepository(db)
        assert repo.job(ws["tenant_id"], job_id) is not None and repo.job(other["tenant_id"], job_id) is None
        assert list(repo.contacts_by_email(ws["tenant_id"], [email.upper()])) == [email.lower()]
        assert repo.contacts_by_email(other["tenant_id"], [email]) == {}
        repo.add(ImportMapping(tenant_id=ws["tenant_id"], name="M", mapping={"E": "contact.email"}))
        db.commit()
        assert repo.mapping_named(ws["tenant_id"], "M") is not None
        assert repo.mapping_named(other["tenant_id"], "M") is None
        rows, total = repo.history(other["tenant_id"], 0, 10)
        assert total == 0 and rows == []
