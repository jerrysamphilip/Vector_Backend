# app/services/ab_testing.py
"""
A/B testing of email subject lines (BR-SF-18 / RP-104) - the pure logic.

No database or network access here, so every rule can be unit tested:

- variant labels (A..D) and their limits;
- the even, deterministic assignment of prospects to variants, honouring
  optional weights and the assignments that already exist;
- re-balancing of not-yet-sent messages when the variant set changes;
- the winner rule: highest unique open rate, once every variant has at least
  MIN_SENDS_PER_VARIANT sent emails.

The database side (who gets which message, stats queries, promotion) lives in
app/services/subject_variant_service.py.
"""

from fractions import Fraction
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

VARIANT_LABELS: Tuple[str, ...] = ("A", "B", "C", "D")
MIN_VARIANTS = 2           # an A/B test runs only with at least two variants
MAX_VARIANTS = 4
MIN_WEIGHT = 1
MAX_WEIGHT = 100
DEFAULT_WEIGHT = 1
MAX_SUBJECT_LENGTH = 500   # same bound as email_templates.subject (EmailTemplateCreate)

# Minimum number of SENT emails every variant needs before a winner is named.
MIN_SENDS_PER_VARIANT = 30

WINNER_NOT_RUNNING = "not_running"        # fewer than two variants
WINNER_INSUFFICIENT = "insufficient_data"  # some variant below the sample size
WINNER_TIE = "tie"                         # two or more variants share the best open rate
WINNER_FOUND = "winner"
WINNER_PROMOTED = "promoted"               # the owner already promoted a variant

Variant = Tuple[str, int]  # (label, weight)


def next_label(existing: Iterable[str]) -> Optional[str]:
    """The first free label A..D, or None when all four are taken."""
    used = {(e or "").upper() for e in existing}
    for label in VARIANT_LABELS:
        if label not in used:
            return label
    return None


def normalize_label(label: str) -> Optional[str]:
    """'a' -> 'A'; anything that is not a known label -> None."""
    value = (label or "").strip().upper()
    return value if value in VARIANT_LABELS else None


def shares(variants: Sequence[Variant]) -> Dict[str, Fraction]:
    """Each variant's share of the traffic (exact fractions; weights default to equal)."""
    clean = [(label, max(int(weight or DEFAULT_WEIGHT), MIN_WEIGHT)) for label, weight in variants]
    total = sum(w for _, w in clean)
    if not clean or total <= 0:
        return {}
    return {label: Fraction(w, total) for label, w in clean}


def _ordered(variants: Sequence[Variant]) -> List[str]:
    return sorted({label for label, _ in variants}, key=VARIANT_LABELS.index)


def _pick(share: Mapping[str, Fraction], counts: Mapping[str, int], order: List[str]) -> str:
    """The variant furthest below its share once one more message is added.

    deficit = share * (total + 1) - count; the largest deficit wins, ties go to
    the earlier label. With equal weights this is plain round-robin A, B, A, B...
    """
    total_after = sum(counts.get(label, 0) for label in order) + 1
    best, best_deficit = order[0], None
    for label in order:
        deficit = share[label] * total_after - counts.get(label, 0)
        if best_deficit is None or deficit > best_deficit:
            best, best_deficit = label, deficit
    return best


def assign_variants(
    keys: Iterable[str],
    variants: Sequence[Variant],
    existing_counts: Optional[Mapping[str, int]] = None,
) -> Dict[str, str]:
    """Assign each key (a prospect id) a variant label, evenly and deterministically.

    keys are processed in sorted order, so the same input always gives the same
    result. existing_counts holds the messages already assigned per label for the
    same step, which the new assignments balance against (so re-planning or
    enrolling more prospects keeps the split even). Returns {} when fewer than
    MIN_VARIANTS variants are given.
    """
    if len(variants) < MIN_VARIANTS:
        return {}
    share = shares(variants)
    order = _ordered(variants)
    counts = {label: int((existing_counts or {}).get(label, 0)) for label in order}
    result: Dict[str, str] = {}
    for key in sorted(set(keys)):
        label = _pick(share, counts, order)
        result[key] = label
        counts[label] += 1
    return result


def targets(variants: Sequence[Variant], total: int) -> Dict[str, int]:
    """Whole-number message counts per variant for `total` messages (largest remainder)."""
    share = shares(variants)
    order = _ordered(variants)
    if not order or total <= 0:
        return {label: 0 for label in order}
    exact = {label: share[label] * total for label in order}
    result = {label: int(exact[label]) for label in order}
    left = total - sum(result.values())
    by_remainder = sorted(order, key=lambda lb: (-(exact[lb] - result[lb]), VARIANT_LABELS.index(lb)))
    for label in by_remainder[:left]:
        result[label] += 1
    return result


def rebalance(
    pending: Mapping[str, Optional[str]],
    variants: Sequence[Variant],
    fixed_counts: Optional[Mapping[str, int]] = None,
) -> Dict[str, str]:
    """Re-balance not-yet-sent messages after the variant set or weights changed.

    pending: {key: current label or None} for the step's unsent messages.
    fixed_counts: messages per label that can no longer move (already sent).
    A pending message keeps its label while that label is still under its target
    share of (sent + pending); the rest are re-assigned with assign_variants.
    Returns {key: label} for every pending key ({} when the test is not running).
    """
    if len(variants) < MIN_VARIANTS:
        return {}
    order = _ordered(variants)
    fixed = {label: int((fixed_counts or {}).get(label, 0)) for label in order}
    goal = targets(variants, sum(fixed.values()) + len(pending))
    counts = dict(fixed)
    result: Dict[str, str] = {}
    free: List[str] = []
    for key in sorted(pending):
        label = normalize_label(pending[key] or "")
        if label in counts and counts[label] < goal[label]:
            result[key] = label
            counts[label] += 1
        else:
            free.append(key)
    result.update(assign_variants(free, variants, counts))
    return result


def rate(numerator: int, denominator: int) -> float:
    """Percentage rounded to 2 decimals; 0.0 when nothing to divide by."""
    return round(numerator * 100.0 / denominator, 2) if denominator else 0.0


def pick_winner(
    stats: Sequence[Mapping],
    min_sends: int = MIN_SENDS_PER_VARIANT,
    promoted_label: Optional[str] = None,
) -> Dict:
    """Decide the winner by unique open rate.

    stats: one mapping per variant with at least label, sent and opened.
    Returns {"status", "label", "reason"}; label is set for 'winner' / 'promoted'.
    """
    if promoted_label:
        return {"status": WINNER_PROMOTED, "label": promoted_label,
                "reason": f"Variant {promoted_label} was promoted; all remaining emails use its subject."}
    if len(stats) < MIN_VARIANTS:
        return {"status": WINNER_NOT_RUNNING, "label": None,
                "reason": f"Add at least {MIN_VARIANTS} subject variants to run an A/B test."}
    short = [s["label"] for s in stats if int(s.get("sent") or 0) < min_sends]
    if short:
        return {"status": WINNER_INSUFFICIENT, "label": None,
                "reason": f"Every variant needs at least {min_sends} sent emails before a winner is named "
                          f"(still short: {', '.join(short)})."}
    open_rates = {s["label"]: Fraction(int(s.get("opened") or 0), int(s["sent"])) for s in stats}
    best = max(open_rates.values())
    best_pct = float(round(best * 100, 2))
    leaders = [label for label, r in open_rates.items() if r == best]
    if len(leaders) > 1:
        return {"status": WINNER_TIE, "label": None,
                "reason": f"Variants {', '.join(sorted(leaders))} have the same open rate ({best_pct}%)."}
    leader = leaders[0]
    return {"status": WINNER_FOUND, "label": leader,
            "reason": f"Variant {leader} has the highest open rate ({best_pct}%) "
                      f"with at least {min_sends} sends per variant."}
