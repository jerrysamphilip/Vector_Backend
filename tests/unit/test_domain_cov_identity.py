"""Coverage of IdentityRuleError's status default (app/domain/identity.py)."""
from app.domain.identity import IdentityRuleError


def test_rule_error_keeps_the_class_status_unless_given_one():
    plain = IdentityRuleError("nope")
    assert plain.status == 400 and plain.message == "nope" and str(plain) == "nope"
    assert IdentityRuleError("gone", 404).status == 404
    assert IdentityRuleError.status == 400
