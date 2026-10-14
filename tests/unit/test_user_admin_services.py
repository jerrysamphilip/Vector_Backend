"""Unit tests (no database) for the pure rules extracted from the user router into
app/services/user_admin_service.py, invitation_service.py and invitation_email.py."""
import string
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


def test_inv_u02_temporary_password_shape():
    for _ in range(20):
        pw = invitation_service.generate_temporary_password()
        assert len(pw) == 12
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in "!@#$%&*" for c in pw)


@pytest.mark.parametrize("sent,env,deployed,expected", [
    (False, "development", False, True),
    (False, " Local ", False, True),
    (True, "development", False, False),
    (False, "production", False, False),
    (False, "dev", True, False),
    (False, None, False, False),
])
def test_inv_u03_credentials_only_for_local_dev_without_email(sent, env, deployed, expected):
    assert invitation_service.may_expose_credentials(sent, env, deployed) is expected


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


def test_inv_u06_text_body_has_credentials():
    body = invitation_email.build_invite_text(recipient_name="Jane", to_email="j@x.com", temporary_password="P@ss1",
                                              magic_login_link="https://l", tenant_name="Acme",
                                              support_email="help@x.com")
    assert "Hi Jane," in body and "Password: P@ss1" in body and "https://l" in body and "help@x.com" in body
