"""
Campaign value objects and pure rules (ADR 0009): duplicate options, enrolment removal, the
lead-list timing columns and template coverage per persona. No I/O, no SQLAlchemy; the repository
(app/repositories/campaigns.py) loads the facts and the services apply these rules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

# stopped_reason of an enrolment the user removed from the campaign (DELETE /campaigns/{id}/prospects/{pid}).
# The row is kept (status PAUSED) for history; it is hidden from the campaign's lists and counts (S-21).
REMOVED_BY_USER = "Removed from campaign by user"

# Unsent step email statuses: the email can still go out
PENDING_MESSAGE_STATUSES = frozenset({"QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"})

DUPLICATE_NAME_PREFIX = "Copy of "
CAMPAIGN_NAME_MAX = 255


def is_removed(status: Optional[str], stopped_reason: Optional[str]) -> bool:
    """True for an enrolment the user removed from the campaign."""
    return status == "PAUSED" and stopped_reason == REMOVED_BY_USER


def duplicate_name(source_name: str, requested: Optional[str] = None) -> str:
    """The copy's name: the requested one, else 'Copy of <source>' (cut to the column length)."""
    name = (requested or "").strip() or f"{DUPLICATE_NAME_PREFIX}{source_name}"
    return name[:CAMPAIGN_NAME_MAX]


@dataclass(frozen=True, slots=True)
class DuplicateOverrides:
    """What the copy changes compared with its source; None = keep the source's value."""

    campaign_name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    send_window_start: Optional[time] = None
    send_window_end: Optional[time] = None
    campaign_timezone: Optional[str] = None
    inbox_ids: Optional[Sequence[str]] = None
    list_ids: Sequence[str] = ()
    prospect_ids: Sequence[str] = ()
    skip_source_prospects: bool = True
    copy_automation_rules: bool = True
    copy_attachments: bool = True

    @property
    def enrols(self) -> bool:
        return bool(self.list_ids or self.prospect_ids)


def copy_start_date(requested: Optional[date], source_start: Optional[date], today: date) -> Optional[date]:
    """A requested start date (never in the past); else the source's when it is still ahead; else none."""
    if requested is not None:
        if requested < today:
            raise ValueError("start_date cannot be in the past")
        return requested
    return source_start if source_start and source_start >= today else None


def copy_end_date(requested: Optional[date], source_end: Optional[date], start: Optional[date],
                  today: date) -> Optional[date]:
    """A requested end date (not before the start); else the source's when it is still ahead of the start."""
    earliest = start or today
    if requested is not None:
        if requested < earliest:
            raise ValueError("end_date must be on or after the start date")
        return requested
    return source_end if source_end and source_end >= earliest else None


def check_send_window(start: Optional[time], end: Optional[time]) -> None:
    if start and end and end <= start:
        raise ValueError("send_window_end must be after send_window_start")


# ── Lead-list timing (F-12, S-22) ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StepMessage:
    """One outbound step email of a contact in a campaign, with its step's stop flags."""

    step_number: int
    status: str
    scheduled_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    stop_on_reply: Optional[bool] = True
    stop_on_bounce: Optional[bool] = True


@dataclass(frozen=True, slots=True)
class LeadTiming:
    last_sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    bounce_reason: Optional[str] = None
    ooo: bool = False
    steps_sent: int = 0
    steps_total: int = 0
    next_email_at: Optional[datetime] = None
    stopped_reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "last_sent_at": self.last_sent_at,
            "delivered_at": self.delivered_at,
            "bounce_reason": self.bounce_reason,
            "ooo": self.ooo,
            "steps_sent": self.steps_sent,
            "steps_total": self.steps_total,
            "next_email_at": self.next_email_at,
            "stopped_reason": self.stopped_reason,
        }


def lead_timing(
    messages: Iterable[StepMessage],
    *,
    steps_total: int,
    stopped_reason: Optional[str],
    ooo: bool,
    sending: bool,
    step_stopped: Callable[[StepMessage], bool],
) -> LeadTiming:
    """
    The lead list's timing columns for one enrolment. ``next_email_at`` is the earliest unsent step
    email that will still go: only while the campaign is sending (``sending``), and skipping steps the
    contact's status stops under that step's own stop_on_reply / stop_on_bounce flags (``step_stopped``,
    app/services/sequence_stop.py).
    """
    msgs = list(messages)
    sent = [m for m in msgs if m.sent_at is not None]
    last_sent = max((m.sent_at for m in sent if m.sent_at), default=None)
    delivered = max((m.delivered_at for m in msgs if m.delivered_at), default=None)
    bounced = sorted((m for m in msgs if m.status == "BOUNCED"),
                     key=lambda m: m.sent_at or datetime.min, reverse=True)
    pending = [m.scheduled_at for m in msgs
               if m.sent_at is None and m.status in PENDING_MESSAGE_STATUSES and m.scheduled_at
               and not step_stopped(m)]
    return LeadTiming(
        last_sent_at=last_sent,
        delivered_at=delivered,
        bounce_reason=(bounced[0].failure_reason or "Bounced") if bounced else None,
        ooo=ooo,
        steps_sent=len({m.step_number for m in sent}),
        steps_total=steps_total,
        next_email_at=min(pending) if (sending and pending) else None,
        stopped_reason=stopped_reason,
    )


# ── Template coverage per persona (S-12 duplicate: persona gaps) ─────────────────────────────────

@dataclass(frozen=True, slots=True)
class PersonaGap:
    persona_type: str
    prospect_count: int
    missing_steps: Sequence[int] = field(default_factory=tuple)


def persona_gaps(
    persona_counts: Mapping[str, int],
    designations_by_step: Mapping[int, set[Optional[str]]],
) -> list[PersonaGap]:
    """
    Personas (with their contact counts) lacking a template for some steps. A step covers a persona
    when it has the persona's own template or a generic one (no designation); otherwise launch would
    fall back to another persona's template.
    """
    gaps = []
    for persona, count in sorted(persona_counts.items()):
        missing = sorted(step for step, names in designations_by_step.items()
                         if persona not in names and None not in names)
        if missing:
            gaps.append(PersonaGap(persona, count, tuple(missing)))
    return gaps


def uncovered_steps(steps: Iterable[tuple[int, str]], template_sequence_ids: Iterable[Optional[str]]) -> list[int]:
    """
    Step numbers of ``steps`` ((step_number, sequence_id) pairs) without a single template. Launch needs one
    per step: a persona lacking its own falls back to the step's generic or any other template (see
    ``persona_gaps``), but a step with none would be skipped for every contact.
    """
    covered = set(template_sequence_ids)
    return sorted(number for number, sequence_id in steps if sequence_id not in covered)


def missing_templates_message(missing: Sequence[int]) -> str:
    """The launch refusal for steps without a template (400)."""
    label = "step" if len(missing) == 1 else "steps"
    return f"Missing email templates for {label} {', '.join(str(n) for n in missing)}"


# ── Campaign list (GET /campaigns) ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CampaignListFilter:
    """Raw list filters; every one optional. ``campaign_name`` is a case-insensitive partial match."""

    status: Optional[str] = None
    created_by: Optional[str] = None
    campaign_name: Optional[str] = None


def total_pages(total: int, page_size: int) -> int:
    """Pages of the campaign list (0 when there is nothing to list, as the API always answered)."""
    return (total + page_size - 1) // page_size
