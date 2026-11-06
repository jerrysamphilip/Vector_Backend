"""Developer machines never do real work on shared data by accident (H4): the API runs no background loops
unless RUN_BACKGROUND_JOBS=true (warning when that happens under Kubernetes), and locally no real SES client
is built without a stand-in endpoint or ALLOW_REAL_SES=true."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app import main as main_mod
from app.adapters import ses as ses_adapter
from app.core.config import Settings, settings
from app.services import email_sender_service as sender_mod
from app.services import platform_admin_service


def test_background_jobs_are_off_in_the_api_by_default():
    assert Settings.model_fields["RUN_BACKGROUND_JOBS"].default is False
    assert Settings.model_fields["ALLOW_REAL_SES"].default is False


def test_api_warns_when_it_runs_the_loops_under_kubernetes(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.main")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    main_mod.warn_background_jobs_in_deployed_api()
    assert not caplog.records
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    main_mod.warn_background_jobs_in_deployed_api()
    assert any(r.levelno == logging.WARNING and "RUN_BACKGROUND_JOBS=true" in r.getMessage() for r in caplog.records)


def test_lifespan_starts_no_loops_unless_enabled(monkeypatch):
    started = []
    monkeypatch.setattr("app.jobs.start_jobs", lambda: started.append(1) or [])
    monkeypatch.setattr(settings, "AUTO_MIGRATE", False)
    monkeypatch.setattr(settings, "RUN_BACKGROUND_JOBS", False)

    async def run():
        async with main_mod.lifespan(main_mod.app):
            pass

    asyncio.run(run())
    assert started == []
    monkeypatch.setattr(settings, "RUN_BACKGROUND_JOBS", True)
    asyncio.run(run())
    assert started == [1]


@pytest.fixture
def local_without_stand_in(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_SES", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "ALLOW_REAL_SES", False)
    built = []
    monkeypatch.setattr(ses_adapter.boto3, "client", lambda service, **kw: built.append(service) or object())
    return built


def test_no_real_ses_client_locally_without_a_stand_in(local_without_stand_in):
    with pytest.raises(ses_adapter.RealSesRefused, match="AWS_ENDPOINT_URL"):
        ses_adapter.build_ses_client()
    with pytest.raises(ses_adapter.RealSesRefused):
        platform_admin_service._get_ses_client()
    assert local_without_stand_in == []


@pytest.mark.parametrize("how", ["endpoint", "ses_endpoint", "allow", "deployed", "kubernetes"])
def test_real_ses_client_allowed(local_without_stand_in, monkeypatch, how):
    if how == "endpoint":
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://ses-mock:5000")
    elif how == "ses_endpoint":
        monkeypatch.setenv("AWS_ENDPOINT_URL_SES", "http://ses-mock:5000")
    elif how == "allow":
        monkeypatch.setattr(settings, "ALLOW_REAL_SES", True)
    elif how == "deployed":
        monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    else:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    ses_adapter.build_ses_client()
    assert local_without_stand_in == ["ses"]


def test_a_refused_campaign_send_is_a_platform_failure_and_stays_queued(local_without_stand_in, monkeypatch):
    sender = sender_mod.EmailSenderService()
    monkeypatch.setattr(sender_mod.providers, "email_transport", ses_adapter.ses_transport)
    ses_adapter.reset_ses_clients()
    with pytest.raises(sender_mod.PlatformEmailFailure) as exc:
        asyncio.run(sender._send_raw({"Source": "a@x.example.com"}, SimpleNamespace(message_id="m1"),
                                     "b@y.example.com"))
    assert exc.value.code == "REAL_SES_REFUSED"
