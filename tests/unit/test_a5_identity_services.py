"""A5: identity services with fake repositories and a fake unit of work (no database)."""
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import (
    BadRequestError,
    ConflictError,
    GoneError,
    NotFoundError,
    PermissionDenied,
    TooManyRequests,
)
from app.core.security import create_access_token, create_mfa_token, decode_access_token, hash_password, hash_token
from app.domain.identity import ROLE_DEFAULT_PERMISSIONS
from app.services import auth_session, invitation_email, platform_admin_service
from app.services.auth_service import AuthService
from app.services.identity_common import AuthenticationFailed
from app.services.invitation_service import InvitationService
from app.services.mfa_service import MfaService
from app.services.platform_admin_service import PlatformAdminService
from app.services.user_admin_service import TeamService
from tests.unit.a5_identity_fakes import FakeUow, tenant, user

PASSWORD = "correct-horse-1"


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Activity rows and audit entries go through the DB session: not in unit tests."""
    monkeypatch.setattr(auth_session, "record_for", lambda db, u: None)


class Throttle:
    def __init__(self):
        self.events = []

    def check(self):
        self.events.append("check")

    def failed(self):
        self.events.append("failed")

    def succeeded(self):
        self.events.append("succeeded")


def _auth(*users, tenants=(tenant(),)):
    uow = FakeUow(users=users, tenants=tenants)
    return AuthService(uow), uow


# ── AuthService ──

def test_login_wrong_password_is_401_and_counts_a_failure():
    svc, uow = _auth(user(email="a@x.com", password_hash=hash_password(PASSWORD)))
    throttle = Throttle()
    with pytest.raises(AuthenticationFailed) as exc:
        svc.login("a@x.com", "wrong-password", throttle)
    assert exc.value.status_code == 401 and exc.value.detail == "Invalid email or password"
    assert throttle.events == ["check", "failed"] and uow.rollbacks == 1 and not uow.tokens.refresh


def test_login_invited_or_suspended_account_is_403_after_the_password_matched():
    svc, _ = _auth(user(email="a@x.com", password_hash=hash_password(PASSWORD), status="INVITED"))
    throttle = Throttle()
    with pytest.raises(PermissionDenied) as exc:
        svc.login("a@x.com", PASSWORD, throttle)
    assert "invitation email" in exc.value.detail and throttle.events == ["check", "succeeded"]
    svc, _ = _auth(user(email="a@x.com", password_hash=hash_password(PASSWORD)),
                   tenants=(tenant(status="SUSPENDED"),))
    with pytest.raises(PermissionDenied) as exc:
        svc.login("a@x.com", PASSWORD, Throttle())
    assert exc.value.detail == "Tenant suspended"


def test_login_issues_a_session_with_a_rotation_family():
    me = user(email="a@x.com", password_hash=hash_password(PASSWORD), role="ADMIN")
    svc, uow = _auth(me)
    result = svc.login("a@x.com", PASSWORD, Throttle())
    assert result["token_type"] == "bearer" and result["first_login"] is False
    assert decode_access_token(result["access_token"])["sub"] == me.user_id
    stored = uow.tokens.refresh[0]
    assert stored.token_hash == hash_token(result["refresh_token"]) and stored.session_id
    assert stored.session_started_at == stored.last_activity_at and me.last_login_at is not None
    assert result["user"]["permissions"] == list(ROLE_DEFAULT_PERMISSIONS["ADMIN"])
    assert uow.commits == 1


def test_login_with_two_factor_hands_out_an_mfa_token_only():
    svc, uow = _auth(user(email="a@x.com", password_hash=hash_password(PASSWORD), mfa_enabled=True))
    result = svc.login("a@x.com", PASSWORD, Throttle())
    assert result["mfa_required"] is True and "access_token" not in result
    assert not uow.tokens.refresh and uow.commits == 1


def test_register_gates_and_conflicts(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SELF_SIGNUP", False)
    svc, _ = _auth()
    throttle = Throttle()
    with pytest.raises(PermissionDenied):
        svc.register("A", "B", "new@x.com", PASSWORD, "WS", throttle)
    assert throttle.events == []  # refused before the rate limit counts the attempt
    monkeypatch.setattr(settings, "ALLOW_SELF_SIGNUP", True)
    if not settings.self_signup_enabled:
        pytest.skip("self sign-up is forced off in this environment")
    svc, _ = _auth(user(email="taken@x.com", status="INACTIVE"))
    with pytest.raises(ConflictError):
        svc.register("A", "B", "taken@x.com", PASSWORD, "WS", Throttle())
    with pytest.raises(BadRequestError):
        svc.register("A", "B", "new@x.com", "short", "WS", Throttle())
    svc, uow = _auth()
    uow.duplicate = True
    with pytest.raises(ConflictError) as exc:
        svc.register("A", "B", "new@x.com", PASSWORD, "WS", Throttle())
    assert exc.value.detail == "Email already registered" and uow.rollbacks == 1


def test_register_creates_the_workspace_and_its_super_admin(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_SELF_SIGNUP", True)
    if not settings.self_signup_enabled:
        pytest.skip("self sign-up is forced off in this environment")
    svc, uow = _auth(tenants=())
    result = svc.register("Ada", "L", "ada@x.com", PASSWORD, "Ada Inc", Throttle())
    created = uow.users.added[0]
    assert (created.role, created.tenant_id) == ("SUPER_ADMIN", uow.tenants.added[0].tenant_id)
    assert uow.tenants.added[0].tenant_name == "Ada Inc" and result["user"]["email"] == "ada@x.com"


def test_magic_login_activates_an_invited_account_and_accepts_its_invitation():
    invited = user(status="INVITED")
    svc, uow = _auth(invited)
    uow.tokens.add_magic_link(invited.user_id, hash_token("tok"), utcnow() + timedelta(hours=1))
    result = svc.magic_login("tok")
    assert result["first_login"] is True and invited.status == "ACTIVE"
    assert uow.invitations.accepted == [(invited.tenant_id, invited.email)]
    assert uow.tokens.magic[0].used_at is not None


def test_magic_login_expired_link_is_spent_and_401():
    svc, uow = _auth(user())
    uow.tokens.add_magic_link("u", hash_token("tok"), utcnow() - timedelta(seconds=1))
    with pytest.raises(AuthenticationFailed) as exc:
        svc.magic_login("tok")
    assert exc.value.detail == "Magic login token has expired" and uow.commits == 1
    assert uow.tokens.magic[0].used_at is not None


def test_refresh_needs_a_token_and_an_active_user():
    svc, uow = _auth()
    with pytest.raises(AuthenticationFailed):
        svc.refresh(None, None)
    assert uow.entered == 0
    gone = user(status="INACTIVE")
    svc, uow = _auth(gone)
    now = utcnow()
    uow.tokens.add_refresh(user_id=gone.user_id, token_hash=hash_token("r"), expires_at=now + timedelta(days=1),
                           session_id="s", session_started_at=now, last_activity_at=now)
    with pytest.raises(AuthenticationFailed) as exc:
        svc.refresh("r", None)
    assert exc.value.detail == "User not found or inactive"


def test_refresh_rotates_within_the_session():
    me = user()
    svc, uow = _auth(me)
    now = utcnow()
    old = uow.tokens.add_refresh(user_id=me.user_id, token_hash=hash_token("r"), expires_at=now + timedelta(days=1),
                                 session_id="fam", session_started_at=now, last_activity_at=now)
    result = svc.refresh("r", None)
    assert old.revoked_reason == "rotated" and uow.tokens.refresh[-1].session_id == "fam"
    assert uow.tokens.refresh[-1].token_hash == hash_token(result["refresh_token"])


def test_logout_revokes_the_refresh_and_access_token():
    me = user()
    svc, uow = _auth(me)
    rt = uow.tokens.add_refresh(user_id=me.user_id, token_hash=hash_token("r"), expires_at=utcnow())
    access = create_access_token(me.user_id, "t1", "AGENT")
    svc.logout("r", access)
    assert rt.revoked_reason == "logout"
    assert decode_access_token(access)["jti"] in uow.tokens.revoked_access and uow.commits == 2
    svc.logout(None, "not-a-jwt")  # invalid tokens are ignored


def test_forgot_password_answers_the_same_and_stores_a_link_for_active_accounts_only(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.password_reset_email.send_password_reset_email",
                        lambda to, link: sent.append((to, link)))
    svc, uow = _auth(user(email="a@x.com"), user(email="gone@x.com", status="INACTIVE"))
    assert svc.forgot_password("gone@x.com", None) == svc.forgot_password("a@x.com", None)
    assert len(uow.tokens.resets) == 1 and [to for to, _ in sent] == ["a@x.com"]


def test_reset_password_rules():
    svc, uow = _auth(user())
    with pytest.raises(BadRequestError):
        svc.reset_password("t", "short")
    assert uow.entered == 0
    uow.tokens.add_password_reset("u", hash_token("t"), utcnow() - timedelta(seconds=1))
    with pytest.raises(BadRequestError) as exc:
        svc.reset_password("t", PASSWORD)
    assert exc.value.detail == "Reset token has expired" and uow.commits == 1
    me = user()
    svc, uow = _auth(me)
    uow.tokens.add_password_reset(me.user_id, hash_token("t"), utcnow() + timedelta(minutes=5))
    assert svc.reset_password("t", PASSWORD) == {"message": "Password reset successful"}
    assert uow.tokens.user_sessions_revoked == [me.user_id] and me.password_hash


def test_change_and_set_password():
    me = user(password_hash=hash_password(PASSWORD))
    svc, uow = _auth(me)
    with pytest.raises(AuthenticationFailed):
        svc.change_password(me, "nope", "new-password-1", False)
    change = svc.change_password(me, PASSWORD, "new-password-1", True)
    assert change.body == {"message": "Password changed successfully"} and change.session["refresh_token"]
    assert uow.tokens.user_sessions_revoked == [me.user_id]
    with pytest.raises(ConflictError):
        svc.set_initial_password(me, "new-password-2", False)
    invited = user(password_hash=None)
    change = svc.set_initial_password(invited, "new-password-2", False)
    assert change.session is None and invited.password_hash


def test_google_sign_in_refuses_to_link_an_existing_password_account(monkeypatch):
    from app.services import auth_service, google_identity
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client")
    monkeypatch.setattr(settings, "SSO_LINK_ALLOWED_DOMAINS", "")
    monkeypatch.setattr(auth_service, "verify_credential", lambda cred, cid: google_identity.GoogleIdentity(
        google_id="g1", email="a@x.com", first_name="A", last_name="B", avatar=""))
    svc, _ = _auth(user(email="a@x.com", password_hash="h"))
    with pytest.raises(ConflictError) as exc:
        svc.google_sign_in("cred")
    assert exc.value.detail == auth_service.GOOGLE_LINK_REFUSED


def test_profile_reports_the_workspace_two_factor_policy():
    me = user(tenant=tenant(require_mfa=True), custom_permissions=["export_data"])
    profile = AuthService.profile(me)
    assert profile["mfa_required"] is True and profile["mfa_setup_required"] is True
    assert profile["permissions"] == ["export_data"] and profile["tenant_name"] == "Acme"
    platform = user(role="PLATFORM_ADMIN", tenant_id=None, tenant=None)
    assert AuthService.profile(platform)["mfa_required"] is False
    assert AuthService.session_state(me)["recovery_codes_remaining"] == 0


# ── MfaService ──

def test_mfa_verify_and_setup_guards():
    svc = MfaService(FakeUow(users=[user()]))
    with pytest.raises(AuthenticationFailed):
        svc.verify("garbage", "123456", None)
    with pytest.raises(BadRequestError):
        svc.verify(create_mfa_token("u-x"), None, None)
    with pytest.raises(AuthenticationFailed):
        svc.verify(create_mfa_token("u-unknown"), "123456", None)
    with pytest.raises(ConflictError):
        svc.setup(user(mfa_enabled=True))
    me = user()
    out = svc.setup(me)
    assert out["secret"] == me.mfa_secret and out["otpauth_uri"].startswith("otpauth://")


def test_mfa_wrong_code_rolls_back_and_tenant_policy_needs_own_two_factor():
    me = user(mfa_secret="JBSWY3DPEHPK3PXP", user_id="mfa-wrong-code-user")
    uow = FakeUow(users=[me], tenants=[tenant()])
    svc = MfaService(uow)
    with pytest.raises(AuthenticationFailed):
        svc.enable(me, "000000")
    assert me.mfa_enabled is False and uow.rollbacks >= 1
    with pytest.raises(BadRequestError):
        svc.set_tenant_policy(me, True)
    assert svc.set_tenant_policy(me, False) == {"require_mfa": False} and uow.commits == 0


# ── TeamService ──

def _team(*users):
    uow = FakeUow(users=users)
    return TeamService(uow), uow


ACTOR = user(user_id="boss", role="SUPER_ADMIN", email="boss@x.com")


def test_team_refuses_self_actions_before_opening_the_unit_of_work():
    svc, uow = _team(ACTOR)
    for call in (lambda: svc.change_role(ACTOR, "boss", "AGENT"), lambda: svc.deactivate(ACTOR, "boss"),
                 lambda: svc.reactivate(ACTOR, "boss"), lambda: svc.delete_permanently(ACTOR, "boss"),
                 lambda: svc.update_permissions(ACTOR, "boss", [])):
        with pytest.raises(BadRequestError):
            call()
    assert uow.entered == 0
    with pytest.raises(NotFoundError):
        svc.change_role(ACTOR, "nobody", "AGENT")


def test_team_member_lookup_is_tenant_scoped():
    other = user(tenant_id="t2")
    svc, _ = _team(ACTOR, other)
    with pytest.raises(NotFoundError):
        svc.permissions_view(ACTOR, other.user_id)
    assert svc.list_members(ACTOR) == [ACTOR]


def test_deactivating_an_invited_member_revokes_the_invitation_and_links():
    invited = user(status="INVITED")
    svc, uow = _team(ACTOR, invited)
    svc.deactivate(ACTOR, invited.user_id)
    assert invited.status == "INACTIVE" and uow.commits == 1
    assert uow.invitations.revoked_pending == [(invited.tenant_id, invited.email)]
    assert uow.tokens.spent == [invited.user_id]


def test_reactivate_conflicts_on_a_live_account_elsewhere_or_a_concurrent_winner():
    gone = user(status="INACTIVE", email="x@x.com")
    svc, uow = _team(ACTOR, gone, user(tenant_id="t9", email="x@x.com", status="ACTIVE"))
    with pytest.raises(ConflictError):
        svc.reactivate(ACTOR, gone.user_id)
    gone2 = user(status="INACTIVE", email="y@x.com")
    svc, uow = _team(ACTOR, gone2)
    uow.duplicate = True
    with pytest.raises(ConflictError):
        svc.reactivate(ACTOR, gone2.user_id)
    assert uow.rollbacks == 1


def test_permanent_delete_blocked_by_business_records_then_purges():
    gone = user(status="INACTIVE")
    svc, uow = _team(ACTOR, gone)
    uow.users.blocking = "campaigns"
    with pytest.raises(ConflictError) as exc:
        svc.delete_permanently(ACTOR, gone.user_id)
    assert "created campaigns" in exc.value.detail and not uow.users.purged
    uow.users.blocking = None
    svc.delete_permanently(ACTOR, gone.user_id)
    assert uow.users.purged == [gone] and uow.commits == 1


def test_permissions_update_and_reset():
    member = user()
    svc, _ = _team(ACTOR, member)
    with pytest.raises(BadRequestError):
        svc.update_permissions(ACTOR, member.user_id, ["fly"])
    assert sorted(svc.update_permissions(ACTOR, member.user_id, ["export_data", "export_data"])) == ["export_data"]
    assert svc.permissions_view(ACTOR, member.user_id)["is_custom"] is True
    assert svc.reset_permissions(ACTOR, member.user_id) == ["manage_campaigns"]
    assert member.custom_permissions is None


# ── InvitationService ──

@pytest.fixture
def mail(monkeypatch):
    sent = []

    def fake_send(**kw):
        sent.append(kw)
        return {"sent": True, "ses_message_id": "m-1"}
    monkeypatch.setattr(invitation_email, "send_invitation_email", fake_send)
    return sent


def test_invite_creates_the_invited_account_link_and_invitation(mail):
    uow = FakeUow(users=[ACTOR], tenants=[tenant()])
    out = InvitationService(uow).invite(ACTOR, "jane.doe@x.com", "MANAGER", None)
    invited = uow.users.added[0]
    assert (invited.status, invited.password_hash, invited.first_name) == ("INVITED", None, "Jane")
    assert uow.tokens.magic[0].user_id == invited.user_id and uow.invitations.rows[0].status == "PENDING"
    assert out["email_delivery"]["sent"] is True and out["invite_token"] is None and out["show_once_link"] is None
    assert mail[0]["tenant_name"] == "Acme" and uow.commits == 1


def test_invite_refuses_members_who_joined_and_live_accounts_elsewhere(mail):
    joined = user(email="j@x.com", status="ACTIVE")
    uow = FakeUow(users=[ACTOR, joined], tenants=[tenant()])
    with pytest.raises(ConflictError) as exc:
        InvitationService(uow).invite(ACTOR, "j@x.com", "AGENT", None)
    assert "already exists in your workspace" in exc.value.detail
    uow = FakeUow(users=[ACTOR, user(tenant_id="t2", email="e@x.com")], tenants=[tenant()])
    with pytest.raises(ConflictError):
        InvitationService(uow).invite(ACTOR, "e@x.com", "AGENT", None)
    admin = user(role="ADMIN")
    with pytest.raises(PermissionDenied):
        InvitationService(FakeUow()).invite(admin, "s@x.com", "SUPER_ADMIN", None)
    assert not mail


def test_resend_is_rate_limited_on_the_database_clock(mail):
    invited = user(status="INVITED")
    uow = FakeUow(users=[ACTOR, invited], tenants=[tenant()])
    uow.invitations.since_last = 20
    with pytest.raises(TooManyRequests) as exc:
        InvitationService(uow).resend(ACTOR, invited.user_id, None)
    assert exc.value.retry_after == 40
    uow.invitations.since_last = 61
    assert InvitationService(uow).resend(ACTOR, invited.user_id, None)["resent"] is True


def test_accept_expired_invitation_is_410_and_marked():
    uow = FakeUow()
    inv = uow.invitations.add(tenant_id="t1", invited_by="boss", email="n@x.com", role="AGENT",
                              token=hash_token("tok"), status="PENDING", expires_at=utcnow() - timedelta(days=1))
    with pytest.raises(GoneError):
        InvitationService(uow).accept("tok", "N", "M", PASSWORD)
    assert inv.status == "EXPIRED" and uow.commits == 1


def test_accept_activates_the_invited_account():
    invited = user(status="INVITED", email="n@x.com")
    uow = FakeUow(users=[invited])
    inv = uow.invitations.add(tenant_id="t1", invited_by="boss", email="n@x.com", role="AGENT",
                              token=hash_token("tok"), status="PENDING", expires_at=utcnow() + timedelta(days=1))
    out = InvitationService(uow).accept("tok", "Nia", "M", PASSWORD)
    assert out["user_id"] == invited.user_id and invited.status == "ACTIVE" and inv.status == "ACCEPTED"
    assert invited.first_name == "Nia" and uow.tokens.spent == [invited.user_id]


def test_revoke_and_list_invitations():
    invited = user(status="INVITED", email="n@x.com")
    uow = FakeUow(users=[invited])
    inv = uow.invitations.add(tenant_id="t1", invited_by="boss", email="n@x.com", role="AGENT", token="h",
                              status="PENDING", expires_at=utcnow() + timedelta(days=1))
    svc = InvitationService(uow)
    with pytest.raises(NotFoundError):
        svc.revoke(user(tenant_id="t2"), inv.invitation_id)  # another workspace's invitation
    svc.revoke(ACTOR, inv.invitation_id)
    assert inv.status == "REVOKED" and invited.status == "INACTIVE"
    with pytest.raises(NotFoundError):
        svc.revoke(ACTOR, inv.invitation_id)
    overdue = uow.invitations.add(tenant_id="t1", invited_by="boss", email="o@x.com", role="AGENT", token="h2",
                                  status="PENDING", expires_at=utcnow() - timedelta(days=1))
    listed = svc.list(ACTOR)
    assert overdue.status == "EXPIRED" and {i["status"] for i in listed} == {"REVOKED", "EXPIRED"}


# ── PlatformAdminService ──

def test_platform_tenant_status_and_filters():
    uow = FakeUow(tenants=[tenant()])
    svc = PlatformAdminService(uow)
    with pytest.raises(BadRequestError):
        svc.update_tenant_status(ACTOR, "t1", "DELETED")
    with pytest.raises(NotFoundError):
        svc.update_tenant_status(ACTOR, "nope", "SUSPENDED")
    assert svc.update_tenant_status(ACTOR, "t1", "SUSPENDED")["tenant_id"] == "t1"
    assert uow.tenants.get("t1").status == "SUSPENDED"
    svc.list_tenants("ac", "suspended")
    svc.list_users(None, "agent", "t1")
    assert uow.platform.calls == [("tenant_summaries", "ac", "SUSPENDED"), ("users", None, "AGENT", "t1")]


def test_platform_create_tenant(monkeypatch):
    sent = []
    monkeypatch.setattr(platform_admin_service, "send_tenant_invite_email",
                        lambda **kw: sent.append(kw) or {"sent": True, "error": None})
    uow = FakeUow(tenants=[tenant(tenant_name="Acme")])
    svc = PlatformAdminService(uow)
    with pytest.raises(BadRequestError):
        svc.create_tenant("  ", "a@x.com", "A", "B", None)
    with pytest.raises(ConflictError):
        svc.create_tenant("ACME", "a@x.com", "A", "B", None)
    out = svc.create_tenant(" Beta ", "a@x.com", " A ", "B", None)
    admin = uow.users.added[0]
    assert (admin.role, admin.status, admin.first_name, admin.password_hash) == ("SUPER_ADMIN", "INVITED", "A", None)
    assert out["tenant_name"] == "Beta" and out["email_sent"] is True and out["magic_login_link"] is None
    assert uow.tokens.magic[0].user_id == admin.user_id and sent[0]["tenant_name"] == "Beta"
    uow.duplicate = True
    with pytest.raises(ConflictError):
        svc.create_tenant("Gamma", "b@x.com", "A", "B", None)
