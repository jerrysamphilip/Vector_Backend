"""Unit tests (no database, no network) for the quote discount approval rules (BR-SF-17 / RP-103).

Covers app/services/quote_approval.py pure helpers: threshold parsing, effective discount,
when approval is required, when a quote may go out, status after an edit, manager chain
and who may decide.
"""
from decimal import Decimal

import pytest
from app.core.errors import BadRequestError

from app.services import quote_approval as qa


# ── DA-U01 threshold parsing ─────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), (0, 0.0), (15, 15.0), ("12.5", 12.5), (100, 100.0), ("15.555", 15.56),
])
def test_da_u01_parse_threshold_accepts_percent_or_off(value, expected):
    assert qa.parse_threshold(value) == expected


@pytest.mark.parametrize("value", [True, False, -0.01, 100.01, "abc", "nan", "inf", [], {}])
def test_da_u02_parse_threshold_rejects_out_of_bounds(value):
    with pytest.raises(BadRequestError) as exc:
        qa.parse_threshold(value)
    assert exc.value.status_code == 400


# ── DA-U03 effective discount ────────────────────────────────

def test_da_u03_effective_discount_is_top_line_discount():
    # 10% on 100, 30% on 2 x 50: overall is 20%, the biggest line discount 30% wins
    assert qa.effective_discount([(1, 100, 10), (2, 50, 30)]) == Decimal("30.00")


def test_da_u04_effective_discount_no_lines_or_zero_price():
    assert qa.effective_discount([]) == Decimal("0.00")
    assert qa.effective_discount([(1, 0, 0)]) == Decimal("0.00")
    # a 100% line on a free item still counts as a 100% discount
    assert qa.effective_discount([(1, 0, 100)]) == Decimal("100.00")


def test_da_u05_effective_discount_accepts_decimals_and_strings():
    assert qa.effective_discount([(Decimal("2"), Decimal("49.50"), Decimal("12.5"))]) == Decimal("12.50")
    assert qa.effective_discount([("1", "10", None)]) == Decimal("0.00")


# ── DA-U06 required / cleared ────────────────────────────────

def test_da_u06_approval_required_only_strictly_above_threshold():
    assert qa.approval_required(20, 15) is True
    assert qa.approval_required(15, 15) is False      # at the limit is fine
    assert qa.approval_required(99, None) is False    # off
    assert qa.approval_required(0.01, 0) is True      # threshold 0: any discount


def test_da_u07_is_cleared():
    assert qa.is_cleared(10, 15, "NOT_REQUIRED", None)
    assert not qa.is_cleared(20, 15, "NOT_REQUIRED", None)
    assert not qa.is_cleared(20, 15, "PENDING", Decimal("20"))
    assert not qa.is_cleared(20, 15, "REJECTED", Decimal("20"))
    assert qa.is_cleared(20, 15, "APPROVED", Decimal("20"))
    assert qa.is_cleared(18, 15, "APPROVED", Decimal("20"))     # lowered after approval
    assert not qa.is_cleared(25, 15, "APPROVED", Decimal("20"))  # raised after approval
    assert qa.is_cleared(50, None, "REJECTED", None)             # approval switched off


# ── DA-U08 status after an edit ──────────────────────────────

@pytest.mark.parametrize("status, effective, threshold, approved, expected", [
    ("APPROVED", 25, 15, 20, "PENDING"),          # raised above the approved discount
    ("APPROVED", 20, 15, 20, "APPROVED"),         # unchanged
    ("APPROVED", 10, 15, 20, "APPROVED"),         # lowered: approval still stands
    ("PENDING", 10, 15, 20, "NOT_REQUIRED"),      # now within the limit: request withdrawn
    ("REJECTED", 10, 15, None, "NOT_REQUIRED"),
    ("REJECTED", 30, 15, None, "REJECTED"),       # still above: must resubmit
    ("NOT_REQUIRED", 30, 15, None, "NOT_REQUIRED"),  # owner still has to submit
    ("PENDING", 40, 15, 30, "PENDING"),
    ("APPROVED", 40, None, 20, "APPROVED"),       # approval off
])
def test_da_u08_status_after_edit(status, effective, threshold, approved, expected):
    assert qa.status_after_edit(status, effective, threshold, approved) == expected


# ── DA-U09 hierarchy ─────────────────────────────────────────

def test_da_u09_manager_chain_walks_up_and_stops_on_cycles():
    managers = {"rep": "mgr", "mgr": "vp", "vp": None, "x": "y", "y": "x"}
    assert qa.manager_chain(managers, "rep") == ["mgr", "vp"]
    assert qa.manager_chain(managers, "vp") == []
    assert qa.manager_chain(managers, "x") == ["y"]
    assert qa.manager_chain(managers, None) == []
    assert qa.manager_chain(managers, "unknown") == []


def test_da_u10_may_decide():
    chain = ["mgr", "vp"]
    assert qa.may_decide("MANAGER", "mgr", "rep", "rep", chain)
    assert qa.may_decide("AGENT", "vp", "rep", "rep", chain)            # hierarchy, whatever the role
    assert not qa.may_decide("MANAGER", "other", "rep", "rep", chain)   # not in the chain
    assert qa.may_decide("ADMIN", "adm", "rep", "rep", [])
    assert not qa.may_decide("ADMIN", "adm", "adm", "adm", [])          # no self-approval
    assert not qa.may_decide("MANAGER", "mgr", "rep", "mgr", chain)     # requester cannot decide
    assert qa.may_decide("SUPER_ADMIN", "own", "own", "own", [])        # workspace owner may
