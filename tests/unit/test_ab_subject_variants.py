"""Unit tests (no database, no network) for A/B testing of subject lines (BR-SF-18 / RP-104).

Covers app/services/ab_testing.py: labels, even and deterministic assignment (equal split,
weights, balancing against existing assignments), re-balancing of unsent emails, rates and
the winner rule with its minimum sample size.
"""
from collections import Counter
from fractions import Fraction

import pytest

from app.services import ab_testing as ab

AB2 = [("A", 1), ("B", 1)]
AB3 = [("A", 1), ("B", 1), ("C", 1)]


# ── AB-U01..U03 labels ───────────────────────────────────────

def test_ab_u01_next_label_fills_first_free_letter():
    assert ab.next_label([]) == "A"
    assert ab.next_label(["A"]) == "B"
    assert ab.next_label(["A", "C"]) == "B"
    assert ab.next_label(["a", "b", "c"]) == "D"
    assert ab.next_label(["A", "B", "C", "D"]) is None


@pytest.mark.parametrize("raw, expected", [("a", "A"), (" D ", "D"), ("E", None), ("", None), ("AB", None)])
def test_ab_u02_normalize_label(raw, expected):
    assert ab.normalize_label(raw) == expected


def test_ab_u03_limits_match_the_spec():
    assert (ab.MIN_VARIANTS, ab.MAX_VARIANTS) == (2, 4)
    assert ab.VARIANT_LABELS == ("A", "B", "C", "D")
    assert ab.MIN_SENDS_PER_VARIANT == 30


# ── AB-U04..U09 assignment ───────────────────────────────────

def test_ab_u04_equal_split_is_round_robin_over_sorted_keys():
    keys = [f"p{i:02d}" for i in range(6)]
    assert ab.assign_variants(reversed(keys), AB2) == {
        "p00": "A", "p01": "B", "p02": "A", "p03": "B", "p04": "A", "p05": "B"}


def test_ab_u05_assignment_is_deterministic_and_order_independent():
    keys = [f"prospect-{i}" for i in range(37)]
    first = ab.assign_variants(keys, AB3)
    assert first == ab.assign_variants(list(reversed(keys)), AB3)
    assert first == ab.assign_variants(keys + keys[:5], AB3)  # duplicates do not shift anything


@pytest.mark.parametrize("n", [1, 2, 3, 10, 31, 100])
@pytest.mark.parametrize("variants", [AB2, AB3, [("A", 1), ("B", 1), ("C", 1), ("D", 1)]])
def test_ab_u06_equal_split_is_even_within_one(n, variants):
    counts = Counter(ab.assign_variants([f"k{i}" for i in range(n)], variants).values())
    sizes = [counts.get(label, 0) for label, _ in variants]
    assert sum(sizes) == n and max(sizes) - min(sizes) <= 1


def test_ab_u07_weights_split_proportionally():
    counts = Counter(ab.assign_variants([f"k{i}" for i in range(100)], [("A", 3), ("B", 1)]).values())
    assert counts == {"A": 75, "B": 25}
    counts = Counter(ab.assign_variants([f"k{i}" for i in range(90)], [("A", 1), ("B", 2)]).values())
    assert counts == {"A": 30, "B": 60}


def test_ab_u08_new_assignments_balance_against_existing_ones():
    # 10 already on A, 4 on B: the next 6 all go to B (10 / 10)
    result = ab.assign_variants([f"n{i}" for i in range(6)], AB2, {"A": 10, "B": 4})
    assert set(result.values()) == {"B"}


def test_ab_u09_fewer_than_two_variants_assigns_nothing():
    assert ab.assign_variants(["x", "y"], []) == {}
    assert ab.assign_variants(["x", "y"], [("A", 1)]) == {}


# ── AB-U10..U13 targets and re-balancing ─────────────────────

def test_ab_u10_targets_largest_remainder():
    assert ab.targets(AB3, 10) == {"A": 4, "B": 3, "C": 3}
    assert ab.targets([("A", 3), ("B", 1)], 10) == {"A": 8, "B": 2}
    assert ab.targets(AB2, 0) == {"A": 0, "B": 0}


def test_ab_u11_rebalance_keeps_assignments_that_fit():
    pending = {"m1": "A", "m2": "B", "m3": "A", "m4": "B"}
    assert ab.rebalance(pending, AB2) == pending


def test_ab_u12_adding_a_variant_moves_only_the_overflow():
    pending = {f"m{i}": ("A" if i % 2 == 0 else "B") for i in range(6)}
    result = ab.rebalance(pending, AB3)
    assert Counter(result.values()) == {"A": 2, "B": 2, "C": 2}
    kept = [k for k in pending if result[k] == pending[k]]
    assert len(kept) == 4  # only two emails changed variant


def test_ab_u13_rebalance_counts_already_sent_and_frees_deleted_labels():
    # 2 sent on A (cannot move); pending: three on A, one on a deleted variant C
    result = ab.rebalance({"m1": "A", "m2": "A", "m3": "A", "m4": "C"}, AB2, {"A": 2})
    assert Counter(result.values()) + Counter({"A": 2}) == {"A": 3, "B": 3}
    assert "C" not in result.values()
    assert ab.rebalance({"m1": "A"}, [("A", 1)]) == {}  # test stopped: nothing assigned


# ── AB-U14..U20 rates and winner ─────────────────────────────

def test_ab_u14_rate():
    assert ab.rate(1, 3) == 33.33
    assert ab.rate(0, 0) == 0.0
    assert ab.rate(30, 30) == 100.0


def _s(label, sent, opened):
    return {"label": label, "sent": sent, "opened": opened}


def test_ab_u15_winner_is_highest_open_rate_with_enough_sends():
    w = ab.pick_winner([_s("A", 30, 9), _s("B", 30, 12)])
    assert w["status"] == ab.WINNER_FOUND and w["label"] == "B"


def test_ab_u16_no_winner_below_minimum_sample():
    w = ab.pick_winner([_s("A", 30, 9), _s("B", 29, 29)])
    assert w["status"] == ab.WINNER_INSUFFICIENT and w["label"] is None and "B" in w["reason"]


def test_ab_u17_minimum_sample_is_configurable():
    assert ab.pick_winner([_s("A", 5, 1), _s("B", 5, 3)], min_sends=5)["label"] == "B"
    assert ab.pick_winner([_s("A", 5, 1), _s("B", 5, 3)], min_sends=6)["status"] == ab.WINNER_INSUFFICIENT


def test_ab_u18_equal_open_rates_are_a_tie():
    w = ab.pick_winner([_s("A", 40, 10), _s("B", 80, 20), _s("C", 40, 5)])
    assert w["status"] == ab.WINNER_TIE and w["label"] is None


def test_ab_u19_single_variant_is_not_a_test():
    assert ab.pick_winner([_s("A", 100, 50)])["status"] == ab.WINNER_NOT_RUNNING


def test_ab_u20_promoted_variant_overrides_the_computation():
    w = ab.pick_winner([_s("A", 1, 0), _s("B", 1, 1)], promoted_label="A")
    assert w == {"status": ab.WINNER_PROMOTED, "label": "A", "reason": w["reason"]}


# ── AB-U21.. open rate over delivered (sent - bounced), as on every other screen ──

def _sb(label, sent, opened, bounced):
    return {"label": label, "sent": sent, "opened": opened, "bounced": bounced}


def test_ab_u21_open_rate_fraction_is_over_delivered():
    assert ab.open_rate_fraction(_sb("A", 30, 9, 0)) == Fraction(9, 30)
    assert ab.open_rate_fraction(_sb("A", 30, 9, 6)) == Fraction(9, 24)
    assert ab.open_rate_fraction(_s("A", 30, 9)) == Fraction(9, 30)  # bounced missing = 0
    assert ab.open_rate_fraction(_sb("A", 30, 0, 30)) == 0             # nothing delivered: 0, no division
    assert ab.open_rate_fraction(_sb("A", 3, 0, 5)) == 0               # never below zero


def test_ab_u22_bounces_can_change_the_winner():
    # over sent A leads (9/30 = 30% vs 8/30 = 26.7%); over delivered B leads (8/24 = 33.3% vs 9/30 = 30%)
    stats = [_sb("A", 30, 9, 0), _sb("B", 30, 8, 6)]
    w = ab.pick_winner(stats)
    assert w["status"] == ab.WINNER_FOUND and w["label"] == "B" and "33.33%" in w["reason"]
    assert ab.pick_winner([_s("A", 30, 9), _s("B", 30, 8)])["label"] == "A"


def test_ab_u23_tie_and_sample_size_with_bounces():
    # 10/40 and 9/36 are both 25% once B's 4 bounces are left out
    w = ab.pick_winner([_sb("A", 40, 10, 0), _sb("B", 40, 9, 4)])
    assert w["status"] == ab.WINNER_TIE
    # the minimum sample still counts sent emails, bounced ones included
    assert ab.pick_winner([_sb("A", 30, 1, 25), _sb("B", 30, 2, 0)])["label"] == "A"
    assert ab.pick_winner([_sb("A", 29, 1, 0), _sb("B", 30, 2, 0)])["status"] == ab.WINNER_INSUFFICIENT
