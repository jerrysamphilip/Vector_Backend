"""A5: pure identity rules and value objects (app/domain/identity.py), no fakes."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import identity as d

NOW = datetime(2026, 10, 27, 12, 0, 0)


def _m(role="AGENT", status="ACTIVE", user_id="u"):
    return SimpleNamespace(user_id=user_id, role=role, status=status)


def _refused(fn, *args):
    with pytest.raises(d.IdentityRuleError) as exc:
        fn(*args)
    assert isinstance(exc.value, ValueError)
    return exc.value


def test_roles_and_permission_sets():
    assert d.TENANT_ROLES == {"SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"}
    assert d.PLATFORM_ADMIN not in d.TENANT_ROLES
    assert d.DEFAULT_PERMISSIONS["AGENT"] == ["manage_campaigns"]
    assert all(d.DEFAULT_PERMISSIONS[r] == list(p) for r, p in d.ROLE_DEFAULT_PERMISSIONS.items())
    assert set(d.DEFAULT_PERMISSIONS["SUPER_ADMIN"]) == d.ALL_PERMISSIONS
    assert d.default_permissions(None) == [] and d.default_permissions("NOBODY") == []
    assert d.effective_permissions("AGENT", None) == ["manage_campaigns"]
    assert d.effective_permissions("AGENT", []) == []  # an empty custom set is a real restriction
    assert d.effective_permissions("AGENT", ["export_data"]) == ["export_data"]
    assert d.unknown_permissions(["manage_team", "fly", "fly"]) == {"fly"}


def test_role_is_checked_and_super_admin_granted_only_by_super_admin():
    assert _refused(d.check_role, "OWNER").message == "Invalid role. Must be one of: ADMIN, AGENT, MANAGER, SUPER_ADMIN"
    d.check_role("MANAGER")
    assert _refused(d.check_can_grant_role, "ADMIN", "SUPER_ADMIN").status == 403
    d.check_can_grant_role("SUPER_ADMIN", "SUPER_ADMIN")
    d.check_can_grant_role("ADMIN", "MANAGER")


def test_states_and_tenant_gate():
    assert d.LIVE_STATUSES == ("ACTIVE", "INVITED")
    assert d.OPEN_INVITATION_STATUSES == ("PENDING", "EXPIRED")
    assert d.BLOCKED_TENANT_STATUSES == {"SUSPENDED", "INACTIVE", "DELETED"}
    assert d.SETTABLE_TENANT_STATUSES == ("ACTIVE", "SUSPENDED")
    assert not d.tenant_blocked(None) and not d.tenant_blocked("ACTIVE")
    assert d.tenant_blocked("suspended") and d.tenant_blocked("DELETED")


def test_nobody_acts_on_themselves():
    assert _refused(d.check_not_self, "me", "me", "Cannot delete yourself").message == "Cannot delete yourself"
    d.check_not_self("me", "you", "x")


def test_role_change_and_permission_update_spare_the_super_admin():
    assert _refused(d.check_role_change, _m("SUPER_ADMIN")).status == 400
    d.check_role_change(_m("ADMIN"))
    assert _refused(d.check_permission_update, _m("SUPER_ADMIN"), []).message == "Cannot restrict Super Admin permissions"
    assert "fly" in _refused(d.check_permission_update, _m("AGENT"), ["fly"]).message
    d.check_permission_update(_m("AGENT"), ["export_data"])


@pytest.mark.parametrize("actor,target,status,message", [
    ("SUPER_ADMIN", _m("SUPER_ADMIN"), 400, "Cannot deactivate the workspace Super Admin"),
    ("ADMIN", _m("ADMIN"), 403, "Admins cannot deactivate other admins"),
    ("SUPER_ADMIN", _m("AGENT", "INACTIVE"), 404, "User not found"),
])
def test_deactivate_refusals(actor, target, status, message):
    err = _refused(d.check_deactivate, _m(actor, user_id="me"), target)
    assert (err.status, err.message) == (status, message)


def test_deactivate_allowed():
    d.check_deactivate(_m("ADMIN"), _m("MANAGER"))
    d.check_deactivate(_m("SUPER_ADMIN"), _m("ADMIN", "INVITED"))


@pytest.mark.parametrize("actor,target,status", [
    ("SUPER_ADMIN", _m("AGENT", "ACTIVE"), 400),
    ("SUPER_ADMIN", _m("AGENT", "INVITED"), 400),
    ("ADMIN", _m("SUPER_ADMIN", "INACTIVE"), 403),
    ("ADMIN", _m("ADMIN", "INACTIVE"), 403),
])
def test_reactivate_refusals(actor, target, status):
    assert _refused(d.check_reactivate, _m(actor), target).status == status


def test_reactivate_and_delete_allowed_cases():
    d.check_reactivate(_m("SUPER_ADMIN"), _m("SUPER_ADMIN", "INACTIVE"))
    d.check_reactivate(_m("ADMIN"), _m("AGENT", "INACTIVE"))
    d.check_permanent_delete(_m("AGENT", "INACTIVE"))
    assert _refused(d.check_permanent_delete, _m("AGENT", "ACTIVE")).message == \
        "User must be deactivated before permanent deletion"
    assert _refused(d.check_permanent_delete, _m("SUPER_ADMIN", "INACTIVE")).status == 400
    assert set(d.BLOCKING_RECORD_KINDS) == {"campaigns", "prospect_lists", "campaign_groups", "template_versions"}


def test_reinvitable_reads_history_only_when_needed():
    asked = []

    def history(invited=True, accepted=False):
        def read():
            asked.append(1)
            return d.InvitationHistory(invited=invited, accepted=accepted)
        return read

    assert d.reinvitable("INVITED", None, history())
    assert not d.reinvitable("ACTIVE", None, history())
    assert not d.reinvitable("INACTIVE", NOW, history())  # signed in once: joined
    assert asked == []
    assert d.reinvitable("INACTIVE", None, history())
    assert not d.reinvitable("INACTIVE", None, history(accepted=True))
    assert not d.reinvitable("INACTIVE", None, history(invited=False))
    assert len(asked) == 3


def test_resend_and_revoke_rules():
    assert _refused(d.check_resend, "SUPER_ADMIN", _m("AGENT", "ACTIVE")).status == 409
    assert _refused(d.check_resend, "ADMIN", _m("SUPER_ADMIN", "INVITED")).status == 403
    d.check_resend("ADMIN", _m("AGENT", "INVITED"))
    assert _refused(d.check_revocable, False, None).status == 404
    assert _refused(d.check_revocable, True, "REVOKED").status == 404
    assert _refused(d.check_revocable, True, "ACCEPTED").message == "Cannot revoke invitation with status: ACCEPTED"
    d.check_revocable(True, "PENDING")
    assert d.is_overdue("PENDING", NOW - timedelta(seconds=1), NOW)
    assert not d.is_overdue("PENDING", NOW, NOW) and not d.is_overdue("ACCEPTED", NOW - timedelta(days=1), NOW)
    assert not d.is_overdue("PENDING", None, NOW)


def test_resend_wait_names_and_show_once():
    assert d.resend_wait_seconds(None) == 0
    assert d.resend_wait_seconds(-5) == d.RESEND_COOLDOWN_SECONDS
    assert d.resend_wait_seconds(45) == 15 and d.resend_wait_seconds(600) == 0
    assert d.derive_name_from_email("jane.doe@acme.com") == ("Jane", "Doe")
    assert d.derive_name_from_email("@acme.com") == ("Team", "Member")
    assert d.may_show_link_once(False, "dev", False)
    assert not d.may_show_link_once(False, "dev", True) and not d.may_show_link_once(True, "dev", False)


def test_password_sign_in_states():
    d.check_password_sign_in("ACTIVE")
    assert _refused(d.check_password_sign_in, "INVITED").message == d.FINISH_JOINING
    for status in ("INACTIVE", "SUSPENDED", None):
        err = _refused(d.check_password_sign_in, status)
        assert (err.status, err.message) == (403, d.SUSPENDED_ACCOUNT)
    assert d.password_too_short("1234567") and not d.password_too_short("12345678")


def test_pick_login_user_prefers_live_then_oldest():
    rows = [SimpleNamespace(user_id="a", status="INACTIVE", created_at=NOW - timedelta(days=9)),
            SimpleNamespace(user_id="c", status="INVITED", created_at=NOW - timedelta(days=9)),
            SimpleNamespace(user_id="b", status="ACTIVE", created_at=NOW)]
    assert d.pick_login_user(rows).user_id == "b"
    assert d.pick_login_user(rows[:2]).user_id == "c"
    assert d.pick_login_user([]) is None


def test_google_linking_rule():
    assert d.email_domain("Pat@ACME.example ") == "acme.example"
    assert d.may_link_google(None, "pat@acme.example", {"acme.example"})
    assert not d.may_link_google(None, "pat@acme.example", set())
    assert not d.may_link_google("g-old", "pat@acme.example", {"acme.example"})
