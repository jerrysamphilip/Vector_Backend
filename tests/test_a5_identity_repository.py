"""A5: the identity repositories against MySQL (app/repositories/identity*.py): tenant isolation, one live account
per email (users.active_email), invitations, refresh-token families, magic links and the revoked access-token
list, the permanent-delete cleanup and the platform reads."""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.security import hash_password, hash_token
from app.domain.identity import InvitationHistory
from app.models.user import User
from app.repositories.identity import DuplicateAccount, SqlAlchemyIdentityUnitOfWork, borrow
from app.services import token_revocation
from tests.conftest import RUN, SessionLocal, register_tenant, sql


def _email(label):
    return f"{label}-{RUN}-{uuid.uuid4().hex[:6]}@a5.example.com"


def _add_user(access, tenant_id, label, status="ACTIVE", role="AGENT", email=None):
    user = access.users.add(tenant_id=tenant_id, first_name=label, last_name="Repo", email=email or _email(label),
                            password_hash=hash_password("x-pass-123"), role=role, status=status,
                            auth_provider="local", email_verified=True)
    access.flush_account()
    return user


@pytest.fixture(scope="module")
def world(client):
    a, b = register_tenant(client, "a5ra"), register_tenant(client, "a5rb")
    return {"a": a["tenant_id"], "b": b["tenant_id"], "owner_a": a["email"]}


def test_member_lookups_are_tenant_scoped(world):
    with SessionLocal() as db:
        access = borrow(db)
        mine = _add_user(access, world["a"], "mine")
        theirs = _add_user(access, world["b"], "theirs")
        db.commit()
        assert access.users.member(world["a"], mine.user_id) is mine
        assert access.users.member(world["a"], theirs.user_id) is None
        assert access.users.team_member(world["b"], mine.user_id) is None
        assert mine.user_id in {u.user_id for u in access.users.members(world["a"])}
        assert theirs.user_id not in {u.user_id for u in access.users.members(world["a"])}
        assert access.users.in_tenant_with_email(world["b"], mine.email) is None
        assert access.users.in_tenant_with_email(world["a"], mine.email, "INVITED") is None
        assert access.users.get(theirs.user_id) is theirs  # global: token subjects


def test_one_live_account_per_email_across_workspaces(world):
    email = _email("live")
    with SessionLocal() as db:
        access = borrow(db)
        first = _add_user(access, world["a"], "live", email=email)
        db.commit()
        with pytest.raises(DuplicateAccount):
            _add_user(access, world["b"], "live", email=email)  # a second ACTIVE account elsewhere
        db.rollback()
        # An INACTIVE account with the address is fine; the live one is found everywhere
        old = _add_user(access, world["b"], "old", status="INACTIVE", email=email.upper())
        db.commit()
        assert access.users.live_with_email(email).user_id == first.user_id
        assert access.users.live_with_email(email, exclude_user_id=first.user_id) is None
        assert {u.user_id for u in access.users.with_email(email)} == {first.user_id, old.user_id}
        assert sql("SELECT active_email FROM users WHERE user_id = :u", u=old.user_id) is None


def test_reactivating_into_a_second_live_account_is_refused_at_commit(world):
    email = _email("react")
    with SessionLocal() as db:
        access = borrow(db)
        _add_user(access, world["a"], "react", email=email)
        old = _add_user(access, world["b"], "react", status="INACTIVE", email=email)
        db.commit()
        uow = SqlAlchemyIdentityUnitOfWork.for_session(db)
        with pytest.raises(DuplicateAccount):
            with uow:
                uow.users.get(old.user_id).status = "ACTIVE"
                uow.commit_account()
        assert sql("SELECT status FROM users WHERE user_id = :u", u=old.user_id) == "INACTIVE"


def test_invitations_are_tenant_scoped_and_report_history(world):
    email = _email("inv")
    with SessionLocal() as db:
        access = borrow(db)
        inviter = sql("SELECT user_id FROM users WHERE email = :e", e=world["owner_a"])
        inv = access.invitations.add(tenant_id=world["a"], invited_by=inviter, email=email, role="AGENT",
                                     token=hash_token(f"tok-{RUN}-{email}"), status="PENDING",
                                     expires_at=utcnow() + timedelta(days=7))
        db.commit()
        assert access.invitations.get(world["b"], inv.invitation_id) is None
        assert access.invitations.pending_for(world["b"], email) is None
        assert access.invitations.pending_by_token(hash_token(f"tok-{RUN}-{email}")).invitation_id == inv.invitation_id
        assert access.invitations.history(world["a"], email) == InvitationHistory(invited=True, accepted=False)
        assert access.invitations.history(world["b"], email) == InvitationHistory(invited=False, accepted=False)
        since = access.invitations.seconds_since_last(world["a"], email)
        assert since is not None and 0 <= since < 120
        assert access.invitations.seconds_since_last(world["b"], email) is None
        assert access.invitations.accept_open(world["b"], email) == 0
        assert access.invitations.accept_open(world["a"], email) == 1
        db.commit()
        assert access.invitations.history(world["a"], email).accepted


def test_refresh_token_family_revocation_and_password_reset_revoke_only_that_user(world):
    with SessionLocal() as db:
        access = borrow(db)
        me = _add_user(access, world["a"], "rt")
        other = _add_user(access, world["a"], "rt2")
        now = utcnow()
        for owner, family in ((me, "fam-a"), (me, "fam-a"), (me, "fam-b"), (other, "fam-c")):
            access.tokens.add_refresh(user_id=owner.user_id, token_hash=hash_token(uuid.uuid4().hex),
                                      expires_at=now + timedelta(days=1), session_id=f"{family}-{RUN}",
                                      session_started_at=now, last_activity_at=now)
        db.commit()
        assert access.tokens.revoke_session(f"fam-a-{RUN}", now, "reuse") == 2
        db.commit()
        assert sql("SELECT COUNT(*) FROM refresh_tokens WHERE session_id = :s AND revoked_reason = 'reuse'",
                   s=f"fam-a-{RUN}") == 2
        assert access.tokens.revoke_user_sessions(me.user_id, now) == 1  # fam-b only: fam-a already revoked
        db.commit()
        assert sql("SELECT COUNT(*) FROM refresh_tokens WHERE user_id = :u AND revoked_at IS NULL",
                   u=other.user_id) == 1


def test_magic_links_are_single_use(world):
    with SessionLocal() as db:
        access = borrow(db)
        me = _add_user(access, world["a"], "ml")
        raw = f"magic-{RUN}-{uuid.uuid4().hex}"
        access.tokens.add_magic_link(me.user_id, hash_token(raw), utcnow() + timedelta(hours=1))
        db.commit()
        assert access.tokens.unused_magic_link(hash_token(raw)) is not None
        assert access.tokens.spend_magic_links(me.user_id, utcnow()) == 1
        db.commit()
        assert access.tokens.unused_magic_link(hash_token(raw)) is None


def test_revoked_access_tokens_are_listed_and_expired_rows_pruned(world):
    jti, stale = f"a5-{RUN}-{uuid.uuid4().hex[:8]}", f"a5-old-{RUN}-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        access = borrow(db)
        access.tokens.revoke_access_token(stale, None, utcnow() - timedelta(minutes=1), utcnow())
        db.commit()
        assert token_revocation.is_revoked(db, stale)
        token_revocation.revoke(db, {"jti": jti, "sub": "someone", "exp": (utcnow() + timedelta(minutes=5)).timestamp()})
        assert token_revocation.is_revoked(db, jti)
        assert not token_revocation.is_revoked(db, stale)  # pruned when the next token was revoked
        assert not token_revocation.is_revoked(db, None)


def test_purge_removes_tokens_and_keeps_the_audit_trail(world):
    with SessionLocal() as db:
        access = borrow(db)
        gone = _add_user(access, world["a"], "purge", status="INACTIVE")
        access.tokens.add_magic_link(gone.user_id, hash_token(uuid.uuid4().hex), utcnow())
        access.tokens.add_password_reset(gone.user_id, hash_token(uuid.uuid4().hex), utcnow())
        db.commit()
        sql("INSERT INTO audit_logs (log_id, tenant_id, user_id, action, entity_type, entity_id) "
            "VALUES (:l, :t, :u, 'X', 'user', :u)", l=str(uuid.uuid4()), t=world["a"], u=gone.user_id)
        assert access.users.blocking_record(gone.user_id) is None
        access.users.purge(access.users.get(gone.user_id))
        db.commit()
        assert sql("SELECT COUNT(*) FROM users WHERE user_id = :u", u=gone.user_id) == 0
        assert sql("SELECT COUNT(*) FROM magic_login_tokens WHERE user_id = :u", u=gone.user_id) == 0
        assert sql("SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = :u", u=gone.user_id) == 0
        assert sql("SELECT COUNT(*) FROM audit_logs WHERE entity_id = :u AND user_id IS NULL", u=gone.user_id) == 1


def test_tenant_flags_name_check_and_platform_reads(world):
    with SessionLocal() as db:
        access = borrow(db)
        flags = access.tenants.flags(world["a"])
        assert flags.status == "ACTIVE" and flags.require_mfa is False
        assert access.tenants.flags("no-such-tenant") is None
        name = sql("SELECT tenant_name FROM tenants WHERE tenant_id = :t", t=world["a"])
        assert access.tenants.name_taken(name.upper()) and not access.tenants.name_taken(f"{name}%")
        rows = {r.tenant_id: r for r in access.platform.tenant_summaries(name, None)}
        assert set(rows) == {world["a"]} and rows[world["a"]].user_count >= 1
        assert access.platform.tenant_summaries(name, "SUSPENDED") == []
        users = access.platform.users(None, None, world["b"])
        assert users and all(u.tenant_id == world["b"] for u, _ in users)
        assert all(u.role != "PLATFORM_ADMIN" for u in access.platform.tenant_users(world["a"]))
        assert access.platform.campaign_count(world["b"]) == 0
        stats = access.platform.stats()
        assert stats.total_tenants >= 2 and stats.total_users >= stats.active_users


def test_totp_step_and_recovery_codes_are_accepted_once(world):
    with SessionLocal() as db:
        access = borrow(db)
        me = _add_user(access, world["a"], "totp")
        db.commit()
        assert access.users.accept_totp_step(me, 100)
        assert not access.users.accept_totp_step(me, 100) and not access.users.accept_totp_step(me, 99)
        assert access.users.replace_recovery_codes(me, None, '["h1"]')
        assert not access.users.replace_recovery_codes(me, None, '["h2"]')
        db.commit()
        assert db.get(User, me.user_id).mfa_recovery_codes == '["h1"]'
