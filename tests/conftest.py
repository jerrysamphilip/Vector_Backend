"""
Shared fixtures for the API test suite.

The suite runs against a real MySQL database (MYSQL_HOST / MYSQL_DATABASE / ... or DATABASE_URL),
which should be an empty, throwaway schema: tests create their own tenants and data.

    pip install -r requirements.txt -r requirements-dev.txt
    MYSQL_DATABASE=vtest ALLOW_SELF_SIGNUP=true python -m pytest -q

The app is driven in-process with FastAPI's TestClient. The lifespan (background schedulers)
is deliberately not started: tests never use the client as a context manager.
"""

import os
import sys
import uuid

# ── Environment, before anything imports app.core.config ──
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("ALLOW_SELF_SIGNUP", "true")
os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-secret-0123456789abcdef0123456789")
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", "test-only-credentials-key")
# No AWS calls from tests: fail fast instead of probing the EC2 metadata endpoint
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402


def _run_migrations() -> None:
    """Bring the schema up to date with the project's migration entry point, when it exists."""
    try:
        from app.db.migrate import main as migrate
    except ImportError:
        return  # older layout: the schema is created when app.main is imported
    argv = sys.argv
    sys.argv = ["migrate"]  # keep pytest's arguments away from any argparse in migrate
    try:
        try:
            rc = migrate()
        except SystemExit as exc:  # argparse/CLI style entry points
            rc = exc.code
        if rc not in (0, None):
            raise RuntimeError(f"app.db.migrate failed (exit code {rc}); see the log above")
    finally:
        sys.argv = argv


_run_migrations()

from app.main import app  # noqa: E402  (also creates/patches the schema on the older layout)
from app.core.database import SessionLocal, engine  # noqa: E402


def _ensure_schema() -> None:
    """Last resort when neither migrate nor app import created the tables."""
    with engine.connect() as conn:
        has_users = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'users'")).scalar()
    if not has_users:
        from app.models import Base
        Base.metadata.create_all(bind=engine)


_ensure_schema()

from fastapi.testclient import TestClient  # noqa: E402

RUN = uuid.uuid4().hex[:8]


# ── Helpers ──

def sql(query: str, **params):
    """Run one SQL statement; returns the first column of the first row (or None) for SELECTs."""
    with engine.begin() as conn:
        result = conn.execute(text(query), params)
        if result.returns_rows:
            row = result.first()
            return row[0] if row else None
    return None


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The in-memory sign-in/register limiter would otherwise throttle a long test run."""
    from app.core.rate_limit import limiter
    with limiter._lock:
        limiter._hits.clear()
    yield


@pytest.fixture(scope="session")
def client():
    # raise_server_exceptions=False: a 500 shows up as a status code, like in production
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def login(client, email: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_tenant(client, label: str) -> dict:
    """Self sign-up: a new tenant with a SUPER_ADMIN owner."""
    from app.core.rate_limit import limiter
    with limiter._lock:
        limiter._hits.clear()
    email = f"owner-{label}-{RUN}@{label}{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    r = client.post("/api/auth/register", json={
        "first_name": label.upper(), "last_name": "Owner", "email": email,
        "password": password, "tenant_name": f"Tenant {label} {RUN}",
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    tenant_id = sql("SELECT tenant_id FROM users WHERE email = :e", e=email)
    assert tenant_id
    return {"email": email, "password": password, "token": token, "tenant_id": tenant_id,
            "headers": auth(token), "label": label}


@pytest.fixture(scope="session")
def tenant_a(client):
    return register_tenant(client, "a")


@pytest.fixture(scope="session")
def tenant_b(client):
    t = register_tenant(client, "b")
    return t


@pytest.fixture(scope="session")
def tenant_a_data(client, tenant_a):
    """Tenant A's campaign, template, inbox, contact, conversation and one sent email.

    Created through the API where an endpoint exists; the conversation and the sent email
    (normally written by the scheduler / IMAP sync) are inserted directly.
    """
    h = tenant_a["headers"]
    domain = f"send-a{RUN}.example.com"
    r = client.post("/inboxes", headers=h, json={
        "email_address": f"sender@{domain}", "smtp_host": "127.0.0.1", "smtp_port": 1,
        "smtp_username": "sender", "smtp_password": "fixture-smtp-pass",
        "imap_host": "127.0.0.1", "imap_port": 1, "imap_username": "sender",
        "imap_password": "fixture-imap-pass", "warmup_enabled": False,
    })
    assert r.status_code == 201, r.text
    inbox_id = r.json()["inbox_id"]

    r = client.post("/campaigns", headers=h, json={"campaign_name": f"Campaign A {RUN}"})
    assert r.status_code == 201, r.text
    campaign_id = r.json()["campaign_id"]

    r = client.post("/templates", headers=h, json={
        "campaign_id": campaign_id, "subject": "Hello {{first_name}}", "body": "Body for A"})
    assert r.status_code == 201, r.text
    template_id = r.json()["template_id"]

    prospect_email = f"prospect.a{RUN}@client{RUN}.example.com"
    r = client.post("/contacts", headers=h, json={"email": prospect_email, "first_name": "Pat"})
    assert r.status_code == 201, r.text
    prospect_id = r.json()["prospect_id"]

    from datetime import datetime
    from app.models.conversation import Conversation
    from app.models.email_message import EmailMessage

    known_link = f"https://www.example.com/landing-{RUN}"
    with SessionLocal() as db:
        conv = Conversation(tenant_id=tenant_a["tenant_id"], prospect_id=prospect_id,
                            inbox_id=inbox_id, subject="Hello Pat")
        db.add(conv)
        db.flush()
        msg = EmailMessage(
            campaign_id=campaign_id, conversation_id=conv.id, prospect_id=prospect_id,
            inbox_id=inbox_id, template_id=template_id, direction="OUTBOUND", status="SENT",
            subject="Hello Pat", body_text=f"Hi Pat, see {known_link}",
            to_email=prospect_email, from_email=f"sender@{domain}", sent_at=datetime.utcnow(),
        )
        db.add(msg)
        db.commit()
        conversation_id, message_id = conv.id, msg.message_id

    return {"inbox_id": inbox_id, "domain": domain, "campaign_id": campaign_id,
            "template_id": template_id, "prospect_id": prospect_id, "prospect_email": prospect_email,
            "conversation_id": conversation_id, "message_id": message_id, "known_link": known_link}
