"""In-memory fakes of the identity repositories and unit of work (A5 service tests)."""
import itertools
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.domain.identity import InvitationHistory
from app.repositories.identity import DuplicateAccount, TenantFlags

_ids = itertools.count(1)


def user(**kw: Any) -> SimpleNamespace:
    """A user row with every attribute the services read."""
    base = dict(
        user_id=f"u{next(_ids)}", tenant_id="t1", first_name="Pat", last_name="Lee", email="pat@acme.example",
        role="AGENT", status="ACTIVE", password_hash=None, auth_provider="local", google_id=None,
        email_verified=True, avatar_url=None, invited_by=None, custom_permissions=None, last_login_at=None,
        created_at=datetime(2026, 1, 1), sales_level=None, manager_id=None, mfa_enabled=False, mfa_secret=None,
        mfa_recovery_codes=None, mfa_enabled_at=None, mfa_last_step=None, tenant=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def tenant(**kw: Any) -> SimpleNamespace:
    base = dict(tenant_id="t1", tenant_name="Acme", status="ACTIVE", require_mfa=False,
                created_at=datetime(2026, 1, 1), updated_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeUsers:
    def __init__(self, rows=()):
        self.rows: List[Any] = list(rows)
        self.added: List[Any] = []
        self.purged: List[Any] = []
        self.blocking: Optional[str] = None
        self.totp_steps: List[int] = []

    def get(self, user_id):
        return next((u for u in self.rows if u.user_id == user_id), None)

    def member(self, tenant_id, user_id):
        return next((u for u in self.rows if u.user_id == user_id and u.tenant_id == tenant_id), None)

    def members(self, tenant_id):
        return [u for u in self.rows if u.tenant_id == tenant_id and u.role != "PLATFORM_ADMIN"]

    def team_member(self, tenant_id, user_id):
        found = self.member(tenant_id, user_id)
        return found if found is not None and found.role != "PLATFORM_ADMIN" else None

    def in_tenant_with_email(self, tenant_id, email, status=None):
        return next((u for u in self.rows if u.tenant_id == tenant_id and u.email == email
                     and (status is None or u.status == status)), None)

    def by_google_id(self, google_id):
        return next((u for u in self.rows if u.google_id == google_id), None)

    def with_email(self, email):
        return [u for u in self.rows if u.email == email]

    def any_with_email(self, email):
        return next(iter(self.with_email(email)), None)

    def live_with_email(self, email, exclude_user_id=None):
        return next((u for u in self.rows if u.email == email and u.status in ("ACTIVE", "INVITED")
                     and u.user_id != exclude_user_id), None)

    def add(self, **fields):
        row = user(**fields)
        self.rows.append(row)
        self.added.append(row)
        return row

    def accept_totp_step(self, u, step):
        if u.mfa_last_step is not None and u.mfa_last_step >= step:
            return False
        u.mfa_last_step = step
        self.totp_steps.append(step)
        return True

    def replace_recovery_codes(self, u, stored, remaining):
        if u.mfa_recovery_codes != stored:
            return False
        u.mfa_recovery_codes = remaining
        return True

    def blocking_record(self, user_id):
        return self.blocking

    def purge(self, u):
        self.purged.append(u)
        self.rows.remove(u)


class FakeTenants:
    def __init__(self, rows=()):
        self.rows: Dict[str, Any] = {t.tenant_id: t for t in rows}
        self.added: List[Any] = []

    def get(self, tenant_id):
        return self.rows.get(tenant_id)

    def flags(self, tenant_id):
        t = self.rows.get(tenant_id)
        return TenantFlags(t.status, bool(t.require_mfa)) if t else None

    def name_taken(self, name):
        return any(t.tenant_name.lower() == name.lower() for t in self.rows.values())

    def add(self, **fields):
        t = tenant(tenant_id=f"t{next(_ids)}", **fields)
        self.rows[t.tenant_id] = t
        self.added.append(t)
        return t


class FakeInvitations:
    def __init__(self, rows=()):
        self.rows: List[Any] = list(rows)
        self.accepted: List[tuple] = []
        self.revoked_pending: List[tuple] = []
        self.since_last: Optional[int] = None
        self.past = InvitationHistory(invited=False, accepted=False)

    def add(self, **fields):
        inv = SimpleNamespace(invitation_id=f"i{next(_ids)}", created_at=None, **fields)
        self.rows.append(inv)
        return inv

    def get(self, tenant_id, invitation_id):
        return next((i for i in self.rows if i.invitation_id == invitation_id and i.tenant_id == tenant_id), None)

    def pending_by_token(self, token_hash):
        return next((i for i in self.rows if i.token == token_hash and i.status == "PENDING"), None)

    def pending_for(self, tenant_id, email):
        return next((i for i in self.rows if i.tenant_id == tenant_id and i.email == email
                     and i.status == "PENDING"), None)

    def for_tenant(self, tenant_id):
        return [i for i in self.rows if i.tenant_id == tenant_id]

    def accept_open(self, tenant_id, email):
        self.accepted.append((tenant_id, email))
        return 1

    def revoke_pending(self, tenant_id, email):
        self.revoked_pending.append((tenant_id, email))
        for i in self.rows:
            if i.tenant_id == tenant_id and i.email == email and i.status == "PENDING":
                i.status = "REVOKED"
        return 1

    def history(self, tenant_id, email):
        return self.past

    def seconds_since_last(self, tenant_id, email):
        return self.since_last

    def delete_sent_by(self, user_id):
        return None


class FakeTokens:
    def __init__(self):
        self.refresh: List[Any] = []
        self.magic: List[Any] = []
        self.resets: List[Any] = []
        self.revoked_access: Dict[str, Any] = {}
        self.spent: List[str] = []
        self.user_sessions_revoked: List[str] = []
        self.sessions_revoked: List[tuple] = []

    def add_refresh(self, **fields):
        row = SimpleNamespace(token_id=f"r{next(_ids)}", revoked_at=None, revoked_reason=None, created_at=None, **fields)
        self.refresh.append(row)
        return row

    def refresh_for_update(self, token_hash):
        return next((r for r in self.refresh if r.token_hash == token_hash), None)

    def active_refresh(self, token_hash):
        return next((r for r in self.refresh if r.token_hash == token_hash and r.revoked_at is None), None)

    def revoke_session(self, session_id, now, reason):
        self.sessions_revoked.append((session_id, reason))
        return 1

    def revoke_user_sessions(self, user_id, now):
        self.user_sessions_revoked.append(user_id)
        return 1

    def add_magic_link(self, user_id, token_hash, expires_at):
        row = SimpleNamespace(user_id=user_id, token_hash=token_hash, expires_at=expires_at, used_at=None)
        self.magic.append(row)
        return row

    def unused_magic_link(self, token_hash):
        return next((m for m in self.magic if m.token_hash == token_hash and m.used_at is None), None)

    def spend_magic_links(self, user_id, now):
        self.spent.append(user_id)
        return 1

    def add_password_reset(self, user_id, token_hash, expires_at):
        row = SimpleNamespace(user_id=user_id, token_hash=token_hash, expires_at=expires_at, used_at=None)
        self.resets.append(row)
        return row

    def unused_password_reset(self, token_hash):
        return next((r for r in self.resets if r.token_hash == token_hash and r.used_at is None), None)

    def access_token_revoked(self, jti):
        return jti in self.revoked_access

    def revoke_access_token(self, jti, user_id, expires_at, now):
        self.revoked_access[jti] = (user_id, expires_at)

    def delete_for_user(self, user_id):
        return None


class FakePlatform:
    def __init__(self):
        self.calls: List[tuple] = []

    def tenant_summaries(self, search, status):
        self.calls.append(("tenant_summaries", search, status))
        return []

    def users(self, search, role, tenant_id):
        self.calls.append(("users", search, role, tenant_id))
        return []


class FakeUow:
    """Counts enter / commit / rollback; ``duplicate`` makes the next account flush / commit hit the unique key."""

    def __init__(self, users=(), tenants=(), invitations=()):
        self.users = FakeUsers(users)
        self.tenants = FakeTenants(tenants)
        self.invitations = FakeInvitations(invitations)
        self.tokens = FakeTokens()
        self.platform = FakePlatform()
        self.session = SimpleNamespace(name="fake-session")
        self.entered = self.commits = self.rollbacks = self.flushes = 0
        self.duplicate = False

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollbacks += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        self.flushes += 1

    def _account(self):
        if self.duplicate:
            raise DuplicateAccount("uq_users_active_email")

    def flush_account(self):
        self._account()
        self.flushes += 1

    def commit_account(self):
        self._account()
        self.commits += 1
