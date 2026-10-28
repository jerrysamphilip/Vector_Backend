"""Test-stack speed-ups for the live suites (docs/TESTING.md):

- app.jobs.run_once: one loop iteration now, under the worker loop's own named lock (waiting for it), which the
  system suites use as a deterministic "one worker cycle" instead of sleeping;
- SEND_RETRY_BASE_SECONDS: the scheduler's retry backoff unit;
- refused_test_settings: the production refusal of both test-stack switches (start-up checks in
  tests/unit/test_production_guards.py run them in a fresh interpreter).
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import jobs
from app.core import config, job_locks
from app.core.clock import utcnow
from app.core.config import settings
from app.services.email_scheduler_service import EmailSchedulerService


# ── run_once ──

@pytest.fixture
def fake_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "try_acquire", lambda name, wait=0: calls.append(("acquire", name, wait)) or "conn")
    monkeypatch.setattr(jobs, "release", lambda conn, name: calls.append(("release", name)))
    monkeypatch.setattr(jobs, "record_job_heartbeat", lambda name: None)
    return calls


def _with_job(monkeypatch, run):
    monkeypatch.setattr(jobs, "all_jobs", lambda: [jobs.Job("unit", 15, run)])


def test_run_once_runs_the_iteration_under_the_lock(monkeypatch, fake_lock):
    ran = []

    async def body():
        ran.append(fake_lock[-1])
        return 2

    _with_job(monkeypatch, body)
    assert asyncio.run(jobs.run_once("unit", wait_seconds=30)) is True
    assert ran == [("acquire", "unit", 30)]                       # ran while the lock was held
    assert fake_lock == [("acquire", "unit", 30), ("release", "unit")]
    assert "unit" in jobs.LAST_RUN


def test_run_once_reports_a_busy_lock_and_does_not_run(monkeypatch, fake_lock):
    ran = []

    async def body():
        ran.append(1)

    _with_job(monkeypatch, body)
    monkeypatch.setattr(jobs, "try_acquire", lambda name, wait=0: None)
    assert asyncio.run(jobs.run_once("unit")) is False
    assert ran == []


def test_run_once_releases_the_lock_when_the_iteration_fails(monkeypatch, fake_lock):
    async def body():
        raise RuntimeError("boom")

    _with_job(monkeypatch, body)
    with pytest.raises(RuntimeError):
        asyncio.run(jobs.run_once("unit"))
    assert fake_lock[-1] == ("release", "unit")


def test_run_once_unknown_job(monkeypatch, fake_lock):
    with pytest.raises(KeyError, match="known: .*webhooks"):
        asyncio.run(jobs.run_once("no-such-loop"))
    assert fake_lock == []


def test_every_worker_loop_can_be_run_once():
    names = [j.name for j in jobs.all_jobs()]
    assert {"scheduler", "send_safety", "sales_jobs", "webhooks", "imap_sync"} <= set(names)
    assert all(jobs.job_by_name(n).name == n for n in names)


def test_try_acquire_passes_the_wait_to_get_lock(monkeypatch):
    seen = {}

    class Conn:
        def execute(self, stmt, params):
            seen.update(sql=str(stmt), **params)
            return SimpleNamespace(scalar=lambda: 1)

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(job_locks, "_get_lock_engine",
                        lambda: SimpleNamespace(connect=Conn, url=SimpleNamespace(database="db")))
    assert isinstance(job_locks.try_acquire("webhooks", 45.9), Conn)
    assert seen["sql"] == "SELECT GET_LOCK(:n, :w)" and seen["w"] == 45 and seen["n"] == "vector:job:webhooks:db"
    job_locks.try_acquire("webhooks")
    assert seen["w"] == 0                                         # the worker loop never waits


# ── SEND_RETRY_BASE_SECONDS ──

def test_scheduler_backoff_uses_the_configured_unit(monkeypatch):
    msg = SimpleNamespace(retry_count=0, max_retries=3, last_error_code=None, status="SENDING", send_key="k",
                          next_retry_at=None, message_id="m1")
    monkeypatch.setattr(settings, "SEND_RETRY_BASE_SECONDS", 1)
    EmailSchedulerService._on_transient(msg, RuntimeError("SES down"))
    assert msg.status == "QUEUED" and msg.retry_count == 1
    assert timedelta(0) < msg.next_retry_at - utcnow() <= timedelta(seconds=2)


def test_scheduler_backoff_default_is_minutes():
    assert settings.SEND_RETRY_BASE_SECONDS == 60


# ── production refusal ──

def _s(env, **kw):
    base = {"RATE_LIMIT_TEST_MULTIPLIER": 1, "SEND_RETRY_BASE_SECONDS": 60}
    base.update(kw)
    return SimpleNamespace(is_production=env == "production", **base)


def test_defaults_are_accepted_everywhere():
    assert config.refused_test_settings(_s("production")) == []
    assert config.refused_test_settings(_s("production", SEND_RETRY_BASE_SECONDS=300)) == []


def test_test_stack_values_are_accepted_outside_production():
    assert config.refused_test_settings(_s("development", RATE_LIMIT_TEST_MULTIPLIER=100, SEND_RETRY_BASE_SECONDS=1)) == []


def test_test_stack_values_are_refused_in_production():
    errors = config.refused_test_settings(_s("production", RATE_LIMIT_TEST_MULTIPLIER=100, SEND_RETRY_BASE_SECONDS=1))
    assert len(errors) == 2
    assert "RATE_LIMIT_TEST_MULTIPLIER" in errors[0] and "SEND_RETRY_BASE_SECONDS" in errors[1]
