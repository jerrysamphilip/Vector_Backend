"""
Warm-up scoring rules (chunk A8, ADR 0009): an inbox's reputation from its last 7 days of warm-up metrics and
the domain's external reputation, the pool tier it earns, and the overview totals. Pure: the warm-up service
(app/services/warmup_service.py) loads the rows and passes them in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

POOL_RANKING = ("FOUNDATION", "GROWTH", "PREMIUM", "ULTRA_PREMIUM")
BASE_SCORE = 58.0
MIN_SCORE, MAX_SCORE = 25.0, 100.0
HISTORY_DAYS = 6  # plus today: a week, so one quiet day does not crash the score

SEVERE_ISSUES = frozenset({"POOL_TOO_SMALL", "SEND_FAILED"})
DEGRADED_ISSUES = frozenset({"SYNC_STALE", "IMAP_NOT_CONFIGURED", "SMTP_NOT_CONFIGURED"})
FOUNDATION_ISSUES = frozenset({"POOL_TOO_SMALL", "SEND_FAILED", "MAILBOX_PAUSED", "SMTP_NOT_CONFIGURED"})
# (minimum score, minimum warm-up day, pool), best first
POOL_THRESHOLDS = ((99, 21, "ULTRA_PREMIUM"), (97, 14, "PREMIUM"), (92, 7, "GROWTH"))


@dataclass(frozen=True)
class WarmupTotals:
    """Engagement summed over a window of daily warm-up metrics."""

    sent: int = 0
    opened: int = 0
    replied: int = 0
    saved: int = 0
    bounced: int = 0
    complaints: int = 0

    @classmethod
    def of(cls, metrics: Iterable[Any]) -> "WarmupTotals":
        rows = list(metrics)
        return cls(
            sent=sum(max(m.actual_sends, 0) for m in rows),
            opened=sum(m.open_count or 0 for m in rows),
            replied=sum(m.reply_count or 0 for m in rows),
            saved=sum(m.saved_from_spam_count or 0 for m in rows),
            bounced=sum(m.bounce_count or 0 for m in rows),
            complaints=sum(m.complaint_count or 0 for m in rows),
        )

    def rate(self, count: int) -> float:
        return (count / max(self.sent, 1)) * 100


def engagement_score(totals: WarmupTotals, warmup_day: Optional[int]) -> float:
    """The base score from warm-up age and engagement (before issues and external reputation)."""
    score = BASE_SCORE
    score += min(warmup_day or 0, 20) * 1.6
    score += min(totals.rate(totals.opened), 95) * 0.18
    score += min(totals.rate(totals.replied), 40) * 0.25
    score += min(totals.saved, 5) * 1.2
    score -= totals.bounced * 18
    score -= totals.complaints * 22
    return score


def issue_penalty(issue_code: Optional[str]) -> float:
    if issue_code in SEVERE_ISSUES:
        return 10.0
    if issue_code in DEGRADED_ISSUES:
        return 4.0
    return 0.0


def blend_external(score: float, external: Any) -> float:
    """Apply the domain's latest external reputation (complaint / bounce rates, provider score)."""
    if external is None:
        return score
    if external.complaint_rate:
        score -= float(external.complaint_rate) * 200
    if external.bounce_rate:
        score -= float(external.bounce_rate) * 120
    if external.reputation_score:
        score = (score * 0.7) + (float(external.reputation_score) * 0.3)
    return score


def reputation_score(metrics: Iterable[Any], warmup_day: Optional[int], issue_code: Optional[str],
                     external: Any = None) -> float:
    """The inbox's reputation, 25-100 (one decimal)."""
    score = engagement_score(WarmupTotals.of(metrics), warmup_day) - issue_penalty(issue_code)
    score = blend_external(score, external)
    return max(MIN_SCORE, min(round(score, 1), MAX_SCORE))


def email_domain(address: str) -> str:
    return address.split("@")[-1].lower() if "@" in address else ""


def pool_for_score(score: float, warmup_day: Optional[int], issue_code: Optional[str]) -> str:
    """The pool tier a score earns; mailboxes with a blocking issue stay in FOUNDATION."""
    if issue_code in FOUNDATION_ISSUES:
        return "FOUNDATION"
    day = warmup_day or 0
    for min_score, min_day, pool in POOL_THRESHOLDS:
        if score >= min_score and day >= min_day:
            return pool
    return "FOUNDATION"


def dominant_pool(pools: Iterable[Optional[str]]) -> str:
    """The highest pool tier among these inboxes."""
    highest = "FOUNDATION"
    for pool in pools:
        if POOL_RANKING.index(pool or "FOUNDATION") > POOL_RANKING.index(highest):
            highest = pool or "FOUNDATION"
    return highest


def display_name(email_address: str) -> str:
    """'jane.doe@x' -> 'Jane Doe'."""
    local = email_address.split("@")[0]
    return " ".join(chunk.capitalize() for chunk in local.replace("_", ".").split(".") if chunk)


EMPTY_OVERVIEW: Dict[str, Any] = {
    "total_accounts": 0, "warmup_enabled": 0, "accounts_with_issues": 0, "average_reputation": 0,
    "current_pool": "FOUNDATION", "pool_growth_score": 0, "planned_today": 0, "warmup_sent_today": 0,
    "warmup_replied_today": 0, "saved_today": 0,
}


def warmup_overview(inboxes: List[Any]) -> Dict[str, Any]:
    """Workspace warm-up totals over its inboxes (the enabled ones count)."""
    if not inboxes:
        return dict(EMPTY_OVERVIEW)
    enabled = [i for i in inboxes if i.warmup_enabled]
    reputations = [i.warmup_reputation or 0 for i in enabled]
    avg_reputation = round(sum(reputations) / len(reputations), 1) if reputations else 0
    pool_score = min(100, round(avg_reputation + (sum(i.warmup_day or 0 for i in enabled) / max(len(enabled), 1)), 0))
    return {
        "total_accounts": len(inboxes),
        "warmup_enabled": len(enabled),
        "accounts_with_issues": sum(1 for i in enabled if i.warmup_issue_code),
        "average_reputation": avg_reputation,
        "current_pool": dominant_pool(i.warmup_pool for i in enabled),
        "pool_growth_score": pool_score,
        "planned_today": sum(i.warmup_daily_target or 0 for i in enabled),
        "warmup_sent_today": sum(i.warmup_today_sent or 0 for i in enabled),
        "warmup_replied_today": sum(i.warmup_today_replied or 0 for i in enabled),
        "saved_today": sum(i.warmup_today_saved or 0 for i in enabled),
    }
