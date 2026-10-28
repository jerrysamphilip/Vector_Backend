"""Unit tests (no database) for the pure rules extracted from the user router into
app/services/user_admin_service.py, invitation_service.py and invitation_email.py."""
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, PermissionDenied
from app.services import invitation_email, invitation_service, user_admin_service


def test_ua_u01_roles_and_grants():
    user_admin_service.validate_role("AGENT")
    with pytest.raises(BadRequestError) as exc:
        user_admin_service.validate_role("OWNER")
    assert exc.value.detail == "Invalid role. Must be one of: ADMIN, AGENT, MANAGER, SUPER_ADMIN"
    with pytest.raises(PermissionDenied):
        user_admin_service.require_can_grant_role(SimpleNamespace(role="ADMIN"), "SUPER_ADMIN")
    user_admin_service.require_can_grant_role(SimpleNamespace(role="SUPER_ADMIN"), "SUPER_ADMIN")
    user_admin_service.require_can_grant_role(SimpleNamespace(role="ADMIN"), "MANAGER")


def test_ua_u02_effective_permissions():
    assert user_admin_service.effective_permissions(SimpleNamespace(role="AGENT", custom_permissions=None)) == \
        ["manage_campaigns"]
    assert user_admin_service.effective_permissions(SimpleNamespace(role="AGENT", custom_permissions=[])) == []
    assert user_admin_service.effective_permissions(SimpleNamespace(role="NOBODY", custom_permissions=None)) == []
    assert user_admin_service.unknown_permissions(["manage_team", "fly"]) == {"fly"}


def test_inv_u01_names_from_email():
    assert invitation_service.derive_name_from_email("jane.doe@acme.com") == ("Jane", "Doe")
    assert invitation_service.derive_name_from_email("solo@acme.com") == ("Solo", "Member")
    assert invitation_service.derive_name_from_email("@acme.com") == ("Team", "Member")
    assert invitation_email.recipient_name_from_email("mary_ann+x@acme.com") == "Mary Ann X"
    assert invitation_email.recipient_name_from_email("@acme.com") == "there"


def test_inv_u02_no_temporary_password_generator():
    # I-13: invitations never generate a password
    assert not hasattr(invitation_service, "generate_temporary_password")
    assert not hasattr(invitation_service, "may_expose_credentials")


@pytest.mark.parametrize("sent,env,deployed,expected", [
    (False, "development", False, True),
    (False, " Local ", False, True),
    (True, "development", False, False),
    (False, "production", False, False),
    (False, "dev", True, False),
    (False, None, False, False),
])
def test_inv_u03_link_shown_once_only_for_local_dev_without_email(sent, env, deployed, expected):
    assert invitation_service.may_show_link_once(sent, env, deployed) is expected


def test_inv_u03b_show_once_refused_in_production(monkeypatch):
    from app.core.config import settings

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert invitation_service.show_once_link("a@x.com", "https://l", email_sent=False) == {
        "email": "a@x.com", "magic_login_link": "https://l"}
    assert invitation_service.show_once_link("a@x.com", "https://l", email_sent=True) is None
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert invitation_service.show_once_link("a@x.com", "https://l", email_sent=False) is None
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert invitation_service.show_once_link("a@x.com", "https://l", email_sent=False) is None


def test_inv_u04_origins():
    assert invitation_email.normalized_origin("https://sales.neutrino-ai.com/app/team") == \
        "https://sales.neutrino-ai.com"
    assert invitation_email.normalized_origin("http://localhost:5173/x") == "http://localhost:5173"
    assert invitation_email.normalized_origin("https://evil.example.com") == ""
    assert invitation_email.normalized_origin("http://[::1") == ""  # malformed
    assert invitation_email.normalized_origin(None) == ""
    assert invitation_email.is_local_url("")
    assert invitation_email.is_local_url("http://127.0.0.1:8000")
    assert not invitation_email.is_local_url("https://api.example.com")


def test_inv_u05_resolve_base_url():
    resolve = invitation_email.resolve_base_url
    assert resolve("http://localhost:8000", "http://localhost:5173", "dev") == "https://sales.neutrino-ai.com"
    assert resolve("http://localhost:8000", "http://localhost:5173", "stage") == \
        "https://outreach360.neutrinoaistudio.com"
    assert resolve("https://api.x.com", "https://app.x.com", "dev") == "https://app.x.com"
    assert resolve("https://api.x.com", "http://localhost:5173", "") == "https://api.x.com"
    assert resolve("http://localhost:8000", "http://localhost:5173", "") == "http://localhost:5173"


def test_inv_u06_bodies_have_link_and_no_password():
    content = dict(recipient_name="Jane", to_email="j@x.com", magic_login_link="https://l/magic-login?token=t",
                   tenant_name="Acme", support_email="help@x.com")
    text_body = invitation_email.build_invite_text(**content)
    html_body = invitation_email.build_invite_html(**content)
    assert "Hi Jane," in text_body and "https://l/magic-login?token=t" in text_body and "help@x.com" in text_body
    assert "https://l/magic-login?token=t" in html_body
    for body in (text_body, html_body):
        assert "Password:" not in body and "temporary" not in body.lower() and "credentials" not in body.lower()


def test_role_defaults_have_one_source_and_agent_manages_campaigns():
    from app.core import auth
    from app.domain import identity

    assert auth.ROLE_DEFAULT_PERMISSIONS["AGENT"] == ("manage_campaigns",)
    for role, perms in auth.ROLE_DEFAULT_PERMISSIONS.items():
        assert user_admin_service.DEFAULT_PERMISSIONS[role] == list(perms)
        assert identity.DEFAULT_PERMISSIONS[role] == list(perms)
        assert auth._DEFAULT_PERMS[role] == frozenset(perms)

    agent = SimpleNamespace(role="AGENT", custom_permissions=None)
    auth.require_permission("manage_campaigns")(current_user=agent)  # plain def (F004): runs in the threadpool
    with pytest.raises(Exception) as exc:
        auth.require_permission("manage_team")(current_user=agent)
    assert getattr(exc.value, "status_code", None) == 403
