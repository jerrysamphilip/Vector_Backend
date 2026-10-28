"""
Editing a campaign after launch (P2 S-04 / F-04) and the planning rules launch shares with it: which
edits are allowed, how a new step list maps onto the existing steps, when each unsent step email
goes out, which persona template a contact gets, and whether the planned volume fits the mailboxes.

Pure: no I/O, no SQLAlchemy. The repositories load the facts, app/services/campaign_edit_service.py
applies these rules inside one unit of work. Errors are ``ValueError`` subclasses; the service turns
them into 400 / 409 responses.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Protocol, TypeVar




class SendCalendar(Protocol):
    """The business calendar and the send-window planner (app/services/campaign_calendar.py adapts
    app/utils/business_calendar.py); passed in so the domain stays pure."""

    def add_business_days(self, moment: datetime, days: int) -> datetime: ...

    def batch_day_start(self, base: datetime, day_offset: int) -> datetime: ...

    def timezone_for_state(self, state: str, default: str) -> Optional[str]: ...

    def place_in_window(self, base: datetime, timezone: str, settings: "ScheduleSettings", index: int,
                        total: int) -> datetime: ...


class StepTemplate(Protocol):
    @property
    def sequence_id(self) -> Optional[str]: ...

    @property
    def designation(self) -> Optional[str]: ...


T = TypeVar("T", bound=StepTemplate)

MAX_STEPS = 7
# Campaign states in which the campaign may be edited; launched ones have step emails built
EDITABLE_STATUSES = frozenset({"DRAFT", "PAUSED", "ACTIVE"})
LAUNCHED_STATUSES = frozenset({"ACTIVE", "PAUSED"})
# Campaign fields baked into every rendered subject / body
CONTENT_FIELDS = frozenset({"sender_name", "sender_title", "cta_link"})
# Campaign fields that decide when an email goes out
SCHEDULE_FIELDS = frozenset({
    "send_window_start", "send_window_end", "respect_timezone", "campaign_timezone", "sending_mode",
    "min_gap_minutes", "batch_size", "batch_gap_minutes", "daily_batch_size", "start_date", "wait_business_days",
})
# Unsent step emails an edit may change: not SENDING (claimed by the sender) and never SENT
EDITABLE_MESSAGE_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")
# Enrolments still in the sequence: a step added after launch goes to them
IN_SEQUENCE_STATUSES = ("ACTIVE", "OPENED")
FOLLOW_UP_MIN_GAP = timedelta(minutes=1)


class EditRefused(ValueError):
    """An edit the rules do not allow (400)."""


class EditConflict(ValueError):
    """An edit that conflicts with what already happened, e.g. deleting a step that was sent (409)."""


# ── the edit request ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TemplateEdit:
    """One persona version of a step: ``template_id`` edits an existing one, without it a version is added."""

    template_id: Optional[str] = None
    designation: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


@dataclass(frozen=True, slots=True)
class StepEdit:
    """A step of the edited sequence: ``sequence_id`` keeps an existing step, without it a step is added."""

    sequence_id: Optional[str] = None
    wait_days: Optional[int] = None
    stop_on_reply: Optional[bool] = None
    stop_on_bounce: Optional[bool] = None
    templates: tuple[TemplateEdit, ...] = ()


@dataclass(frozen=True, slots=True)
class CampaignEdit:
    """Settings changes (only the fields sent), mailboxes (None = unchanged) and the step list (None = unchanged)."""

    settings: Mapping[str, Any] = field(default_factory=dict)
    inbox_ids: Optional[tuple[str, ...]] = None
    steps: Optional[tuple[StepEdit, ...]] = None
    expected_version: Optional[int] = None


def check_editable(status: Optional[str]) -> None:
    if (status or "") not in EDITABLE_STATUSES:
        raise EditConflict(f"A {(status or 'unknown').lower()} campaign can't be edited")


def changed_settings(current: Mapping[str, Any], requested: Mapping[str, Any]) -> dict[str, Any]:
    """The requested settings whose value differs from the current one."""
    return {name: value for name, value in requested.items() if current.get(name) != value}


def check_settings(status: Optional[str], changed: Mapping[str, Any], merged: Mapping[str, Any]) -> None:
    """Start date locked after launch; send window in order; end date not before the start; known timezone."""
    if status in LAUNCHED_STATUSES and "start_date" in changed:
        raise EditRefused("The start date can't be changed once a campaign has been launched")
    start, end = merged.get("send_window_start"), merged.get("send_window_end")
    if start and end and end <= start:
        raise EditRefused("send_window_end must be after send_window_start")
    if merged.get("start_date") and merged.get("end_date") and merged["end_date"] < merged["start_date"]:
        raise EditRefused("end_date must be on or after start_date")
    if "campaign_timezone" in changed:
        zone(merged.get("campaign_timezone"), strict=True)


def zone(name: Optional[str], strict: bool = False) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        if strict:
            raise EditRefused(f"Unknown timezone: {name}") from exc
        return zoneinfo.ZoneInfo("UTC")


# ── step list ────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StepPlan:
    keep: tuple[tuple[str, int, StepEdit], ...] = ()       # (sequence_id, new step number, edit)
    add: tuple[tuple[int, StepEdit], ...] = ()             # (new step number, edit)
    delete: tuple[str, ...] = ()                           # sequence ids
    renumbered: bool = False

    @property
    def changes_order(self) -> bool:
        return bool(self.add or self.delete or self.renumbered)


def plan_steps(existing: Sequence[tuple[str, int]], requested: Sequence[StepEdit]) -> StepPlan:
    """
    Map the requested step list (in order) onto the existing steps ``(sequence_id, step_number)``.
    Existing steps keep their relative order (a reorder would change what a contact already got);
    new steps may go anywhere; existing steps left out are deleted. 1..7 steps.
    """
    if not requested:
        raise EditRefused("A campaign needs at least one step")
    if len(requested) > MAX_STEPS:
        raise EditRefused(f"Maximum {MAX_STEPS} steps allowed per campaign")
    known = {sid: number for sid, number in existing}
    kept_ids = [s.sequence_id for s in requested if s.sequence_id]
    unknown = [sid for sid in kept_ids if sid not in known]
    if unknown:
        raise EditRefused(f"Unknown step: {unknown[0]}")
    if len(set(kept_ids)) != len(kept_ids):
        raise EditRefused("A step appears twice")
    if [known[sid] for sid in kept_ids] != sorted(known[sid] for sid in kept_ids):
        raise EditRefused("Existing steps can't be reordered; delete a step and add a new one instead")
    keep, add = [], []
    for number, step in enumerate(requested, start=1):
        if step.sequence_id:
            keep.append((step.sequence_id, number, step))
        else:
            add.append((number, step))
    delete = tuple(sid for sid, _ in existing if sid not in set(kept_ids))
    renumbered = any(known[sid] != number for sid, number, _ in keep)
    return StepPlan(tuple(keep), tuple(add), delete, renumbered)


def renumber(current_step: Optional[int], old_to_new: Mapping[int, int]) -> int:
    """An enrolment's current step after steps were added / deleted: the nearest surviving step at or after it."""
    step = current_step or 1
    for old in sorted(old_to_new):
        if old >= step:
            return old_to_new[old]
    return max(old_to_new.values(), default=1)


# ── persona templates ────────────────────────────────────────────────────────────────────────────

TemplateKey = tuple[Optional[str], Optional[str]]


def build_template_map(templates: Iterable[T]) -> dict[TemplateKey, T]:
    """(sequence_id, designation) -> template, plus (sequence_id, None) for a step's generic template."""
    out: dict[TemplateKey, T] = {}
    for t in templates:
        out[(t.sequence_id, t.designation)] = t
        if not t.designation:
            out[(t.sequence_id, None)] = t
    return out


def pick_template(template_map: Mapping[TemplateKey, T], sequence_id: Optional[str],
                  persona_type: Optional[str]) -> Optional[T]:
    """The step's template for the persona, else its generic one, else any of the step's templates."""
    template = template_map.get((sequence_id, persona_type)) or template_map.get((sequence_id, None))
    if template:
        return template
    return next((t for key, t in template_map.items() if key[0] == sequence_id), None)


# ── schedule ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    """The campaign settings that decide send times (see Campaign)."""

    timezone: str = "UTC"
    window_start: Optional[time] = None
    window_end: Optional[time] = None
    sending_mode: str = "spread"
    min_gap_minutes: int = 2
    batch_size: Optional[int] = None
    batch_gap_minutes: int = 30
    daily_batch_size: Optional[int] = None
    respect_timezone: bool = True
    wait_business_days: bool = False
    start_date: Optional[date] = None
    calendar: Optional[SendCalendar] = field(default=None, compare=False, repr=False)

    @property
    def cal(self) -> SendCalendar:
        if self.calendar is None:
            raise ValueError("ScheduleSettings needs a calendar to plan send times")
        return self.calendar

    @classmethod
    def of(cls, campaign: Any, calendar: Optional[SendCalendar] = None) -> "ScheduleSettings":
        return cls(calendar=calendar,
            timezone=campaign.campaign_timezone or "UTC", window_start=campaign.send_window_start,
            window_end=campaign.send_window_end, sending_mode=campaign.sending_mode or "spread",
            min_gap_minutes=max(int(campaign.min_gap_minutes or 0), 0), batch_size=campaign.batch_size,
            batch_gap_minutes=max(int(campaign.batch_gap_minutes or 30), 1),
            daily_batch_size=campaign.daily_batch_size, respect_timezone=bool(campaign.respect_timezone),
            wait_business_days=bool(getattr(campaign, "wait_business_days", False)), start_date=campaign.start_date,
        )


def wait_until(start: datetime, days: int, settings: ScheduleSettings) -> datetime:
    """``start`` plus ``days`` calendar days, or business days (weekends and US holidays skipped)."""
    days = max(int(days or 0), 0)
    if settings.wait_business_days and days:
        return settings.cal.add_business_days(start, days)
    return start + timedelta(days=days)


def schedule_base(now: datetime, settings: ScheduleSettings) -> datetime:
    """Now, or the campaign's start date (midnight in its timezone, as naive UTC) when later."""
    if not settings.start_date:
        return now
    tz = zone(settings.timezone)
    start = datetime.combine(settings.start_date, time.min, tzinfo=tz)
    start_utc = start.astimezone(zoneinfo.ZoneInfo("UTC")).replace(tzinfo=None)
    return max(now, start_utc)


def fresh_start(base: datetime, settings: ScheduleSettings, fresh_index: int) -> datetime:
    """Start of a not-yet-contacted contact: the daily batch throttle puts every N contacts on the next business day."""
    if settings.daily_batch_size and settings.daily_batch_size > 0:
        return settings.cal.batch_day_start(base, fresh_index // settings.daily_batch_size)
    return base


def start_of_local_day(moment: datetime, timezone: str) -> datetime:
    """Midnight (campaign timezone) of the day ``moment`` (naive UTC) falls on, as naive UTC."""
    utc = zoneinfo.ZoneInfo("UTC")
    local = moment.replace(tzinfo=utc).astimezone(zone(timezone))
    return datetime.combine(local.date(), time.min, tzinfo=zone(timezone)).astimezone(utc).replace(tzinfo=None)


def contact_timezone(settings: ScheduleSettings, state: Optional[str]) -> str:
    if settings.respect_timezone and state:
        return settings.cal.timezone_for_state(state, settings.timezone) or settings.timezone
    return settings.timezone


def place_in_window(base: datetime, tz: str, settings: ScheduleSettings, index: int, total: int) -> datetime:
    return settings.cal.place_in_window(base, tz, settings, index, total)


@dataclass(frozen=True, slots=True)
class StepMail:
    """One contact's email of one step, as far as scheduling is concerned."""

    message_id: str
    status: str
    sent_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None

    @property
    def editable(self) -> bool:
        return self.sent_at is None and self.status in EDITABLE_MESSAGE_STATUSES

    @property
    def anchors(self) -> bool:
        """Whether the next step's wait counts from this email (it went or will go out)."""
        return self.sent_at is not None or self.status in EDITABLE_MESSAGE_STATUSES or self.status == "SENDING"


@dataclass(frozen=True, slots=True)
class ContactPlan:
    """Input of plan_contact: the steps in order with their wait days and this contact's emails."""

    steps: Sequence[tuple[str, int]]
    mails: Mapping[str, StepMail]
    timezone: str
    index: int
    total: int
    fresh_base: datetime


def _anchor_time(mail: StepMail, now: datetime) -> datetime:
    return mail.sent_at or mail.scheduled_at or now


def plan_contact(plan: ContactPlan, settings: ScheduleSettings, now: datetime,
                 only: Optional[set[str]] = None) -> dict[str, datetime]:
    """
    New send times for one contact's unsent step emails (all of them, or the ``only`` message ids), in
    step order: each follows the previous step's email (sent or planned) by the step's wait days, a
    contact with no earlier email starts at ``fresh_base``; never in the past; then placed in the send
    window like at launch. Steps without an email keep adding their wait days.
    """
    out: dict[str, datetime] = {}
    prev: Optional[datetime] = None
    pending_wait = 0
    for sequence_id, wait_days in plan.steps:
        pending_wait += max(int(wait_days or 0), 0)
        mail = plan.mails.get(sequence_id)
        if mail is None:
            continue
        movable = mail.editable and (only is None or mail.message_id in only)
        if not movable:
            if mail.anchors:
                prev, pending_wait = out.get(mail.message_id) or _anchor_time(mail, now), 0
            continue
        if prev is None:
            base = wait_until(plan.fresh_base, pending_wait, settings)
        else:
            base = max(wait_until(start_of_local_day(prev, settings.timezone), pending_wait, settings), prev + FOLLOW_UP_MIN_GAP)
        when = place_in_window(max(base, now), plan.timezone, settings, plan.index, plan.total)
        if prev is not None and when <= prev:
            when = prev + FOLLOW_UP_MIN_GAP
        out[mail.message_id] = when
        prev, pending_wait = when, 0
    return out


def launch_times(steps: Sequence[tuple[str, int]], existing: set[str], base: datetime, timezone: str,
                 settings: ScheduleSettings, index: int, total: int) -> dict[str, datetime]:
    """
    Launch-time send times of one contact's step emails that do not exist yet (pre-scheduling): step N
    goes the cumulative wait days after the contact's start, and never sooner than its wait after the
    previous planned step (the rule the scheduler applies after a real send, B01); then placed in the
    send window. Steps that already have an email are skipped (they do not anchor the next one).
    """
    out: dict[str, datetime] = {}
    cumulative = 0
    prev: Optional[datetime] = None
    for sequence_id, wait_days in steps:
        wait = int(wait_days or 0)
        cumulative += wait
        if sequence_id in existing:
            continue
        step_base = wait_until(base, cumulative, settings)
        if prev is not None:
            step_base = max(step_base, wait_until(prev, wait, settings))
        prev = out[sequence_id] = place_in_window(step_base, timezone, settings, index, total)
    return out


def starts_fresh(steps: Sequence[tuple[str, int]], mails: Mapping[str, StepMail]) -> bool:
    """A contact none of whose step emails went out yet (counts against the daily batch throttle)."""
    return not any(m.sent_at is not None or m.status == "SENDING" for m in mails.values()) and any(
        sid in mails for sid, _ in steps)


def passed_step(steps: Sequence[str], step_id: str, sent_steps: set[str]) -> bool:
    """Whether a contact already got a step after ``step_id`` (then a step added there is not sent to them)."""
    later = steps[steps.index(step_id) + 1:] if step_id in steps else []
    return any(sid in sent_steps for sid in later)


# ── pacing (launch) ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PacingWarning:
    planned_peak_per_day: int
    capacity_per_day: int
    peak_day: date
    days_over_capacity: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"planned_peak_per_day": self.planned_peak_per_day, "capacity_per_day": self.capacity_per_day,
                "peak_day": self.peak_day.isoformat(), "days_over_capacity": self.days_over_capacity,
                "message": self.message}


def window_minutes(start: Optional[time], end: Optional[time]) -> int:
    if not start or not end:
        return 24 * 60
    return max((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute), 0)


def inbox_daily_capacity(daily_limit: Optional[int], delay_seconds: Optional[int], minutes: int) -> int:
    """What one mailbox can send in a day's window: its daily limit, capped by window length / its delay."""
    limit = max(int(daily_limit or 0), 0)
    delay = max(int(delay_seconds or 0), 1)
    return min(limit, (minutes * 60) // delay)


def pacing_warning(per_day: Mapping[date, int], capacities: Sequence[int]) -> Optional[PacingWarning]:
    """A warning when some day plans more emails than the campaign's mailboxes can send in the window."""
    if not per_day:
        return None
    capacity = sum(max(c, 0) for c in capacities)
    over = {day: n for day, n in per_day.items() if n > capacity}
    if not over:
        return None
    peak_day = max(over, key=lambda d: (over[d], -d.toordinal()))
    peak = over[peak_day]
    return PacingWarning(
        planned_peak_per_day=peak, capacity_per_day=capacity, peak_day=peak_day, days_over_capacity=len(over),
        message=(f"{peak} emails are planned on {peak_day.isoformat()} but the selected mailboxes can send about "
                 f"{capacity} a day in the send window; the rest will go out on later days. Add mailboxes, widen "
                 f"the window or lower the daily batch size to keep to the plan."),
    )
