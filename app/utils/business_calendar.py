# app/utils/business_calendar.py
"""
Business Calendar Utility.
Handles US holiday detection and timezone-aware scheduling based on prospect's US state.
Uses the `holidays` library for US federal holiday support.
"""

import holidays
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from typing import Optional, Tuple
import logging
import random

logger = logging.getLogger(__name__)

# Default settings (US-based)
DEFAULT_TIMEZONE = "America/New_York"

# Jitter range for anti-spam (in minutes)
SEND_TIME_JITTER_MIN = 0
SEND_TIME_JITTER_MAX = 5

# US State 2-letter code to Timezone mapping
US_STATE_TIMEZONES = {
    # Eastern Time
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "IN": "America/New_York", "KY": "America/New_York",
    "MA": "America/New_York", "MD": "America/New_York", "ME": "America/New_York",
    "MI": "America/New_York", "NC": "America/New_York", "NH": "America/New_York",
    "NJ": "America/New_York", "NY": "America/New_York", "OH": "America/New_York",
    "PA": "America/New_York", "RI": "America/New_York", "SC": "America/New_York",
    "TN": "America/New_York", "VA": "America/New_York", "VT": "America/New_York",
    "WV": "America/New_York", "DC": "America/New_York",
    # Central Time
    "AL": "America/Chicago", "AR": "America/Chicago", "IA": "America/Chicago",
    "IL": "America/Chicago", "KS": "America/Chicago", "LA": "America/Chicago",
    "MN": "America/Chicago", "MO": "America/Chicago", "MS": "America/Chicago",
    "ND": "America/Chicago", "NE": "America/Chicago", "OK": "America/Chicago",
    "SD": "America/Chicago", "TX": "America/Chicago", "WI": "America/Chicago",
    # Mountain Time
    "AZ": "America/Phoenix", "CO": "America/Denver", "MT": "America/Denver",
    "NM": "America/Denver", "UT": "America/Denver", "WY": "America/Denver",
    "ID": "America/Denver",
    # Pacific Time
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    # Alaska & Hawaii
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}

# Full state name to 2-letter code mapping (for robust parsing)
US_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    # Common abbreviations
    "wash": "WA", "calif": "CA", "colo": "CO", "conn": "CT", "mass": "MA",
    "mich": "MI", "minn": "MN", "miss": "MS", "mont": "MT", "nebr": "NE",
    "okla": "OK", "oreg": "OR", "penn": "PA", "tenn": "TN", "virg": "VA",
}

# US holidays instance (will be created per year as needed)
_us_holidays_cache = {}


def _get_us_holidays(year: int) -> holidays.US:
    """Get or create US holidays instance for a given year."""
    if year not in _us_holidays_cache:
        _us_holidays_cache[year] = holidays.US(years=year)
    return _us_holidays_cache[year]


def normalize_state_code(state: Optional[str]) -> Optional[str]:
    """
    Normalize a state input to a 2-letter code.
    
    Handles:
    - 2-letter codes: "NY" -> "NY"
    - Full names: "New York" -> "NY"
    - Mixed case: "new york", "NEW YORK" -> "NY"
    - Common abbreviations: "Calif" -> "CA"
    
    Args:
        state: State input (code, full name, or abbreviation)
    
    Returns:
        2-letter state code or None if not recognized
    """
    if not state:
        return None
    
    state_cleaned = state.strip()
    
    # If it's already a 2-letter code
    if len(state_cleaned) == 2:
        code = state_cleaned.upper()
        if code in US_STATE_TIMEZONES:
            return code
    
    # Try full name or abbreviation lookup
    state_lower = state_cleaned.lower()
    if state_lower in US_STATE_NAME_TO_CODE:
        return US_STATE_NAME_TO_CODE[state_lower]
    
    # Last resort: take first 2 characters and check
    if len(state_cleaned) >= 2:
        code = state_cleaned[:2].upper()
        if code in US_STATE_TIMEZONES:
            return code
    
    logger.warning(f"Could not normalize state '{state}' to a valid code")
    return None


def get_timezone_for_state(state: Optional[str] = None, timezone: Optional[str] = None) -> str:
    """
    Resolve timezone for a prospect based on their US state.
    
    Priority:
    1. Explicit timezone if provided and valid
    2. US state mapping (handles full names and codes)
    3. Default (America/New_York)
    
    Args:
        state: US state code or full name (e.g., "CA", "NY", "California", "New York")
        timezone: Explicit timezone string (e.g., "America/Los_Angeles")
    
    Returns:
        IANA timezone string
    """
    # Priority 1: Explicit timezone
    if timezone:
        try:
            ZoneInfo(timezone)  # Validate timezone
            return timezone
        except Exception:
            logger.warning(f"Invalid timezone '{timezone}', falling back to state mapping")
    
    # Priority 2: US state mapping (with normalization)
    state_code = normalize_state_code(state)
    if state_code and state_code in US_STATE_TIMEZONES:
        return US_STATE_TIMEZONES[state_code]
    
    # Priority 3: Default
    return DEFAULT_TIMEZONE


def is_us_holiday(dt: datetime) -> bool:
    """
    Check if a date is a US federal holiday.
    
    Args:
        dt: Date to check
    
    Returns:
        True if the date is a US holiday
    """
    try:
        us_holidays = _get_us_holidays(dt.year)
        return dt.date() in us_holidays
    except Exception as e:
        logger.warning(f"Error checking US holiday: {e}")
        return False


def get_holiday_name(dt: datetime) -> Optional[str]:
    """
    Get the name of the US holiday on a given date.
    
    Args:
        dt: Date to check
    
    Returns:
        Holiday name or None if not a holiday
    """
    try:
        us_holidays = _get_us_holidays(dt.year)
        return us_holidays.get(dt.date())
    except Exception:
        return None


def is_weekend(dt: datetime) -> bool:
    """
    Check if date is a weekend day (Saturday or Sunday).
    
    Args:
        dt: Date to check
    
    Returns:
        True if Saturday (5) or Sunday (6)
    """
    return dt.weekday() >= 5


def is_business_day(dt: datetime) -> bool:
    """
    Check if a date is a US business day (not weekend, not US holiday).
    
    Args:
        dt: Date to check
    
    Returns:
        True if it's a valid business day
    """
    return not is_weekend(dt) and not is_us_holiday(dt)


def get_next_business_day(dt: datetime, max_iterations: int = 30) -> Tuple[datetime, Optional[str]]:
    """
    Get the next US business day (skipping weekends and US holidays).
    Returns the same date if it's already a business day.
    
    Args:
        dt: Starting date
        max_iterations: Maximum days to look ahead (prevents infinite loop)
    
    Returns:
        Tuple of (next_business_day, skip_reason)
        skip_reason is None if no skip occurred, otherwise describes why
    """
    original_date = dt
    skip_reasons = []
    
    for _ in range(max_iterations):
        if is_business_day(dt):
            break
        
        # Log why we're skipping
        if is_weekend(dt):
            day_name = "Saturday" if dt.weekday() == 5 else "Sunday"
            skip_reasons.append(f"{day_name} {dt.strftime('%Y-%m-%d')}")
        elif is_us_holiday(dt):
            holiday_name = get_holiday_name(dt) or "US Holiday"
            skip_reasons.append(f"{holiday_name} ({dt.strftime('%Y-%m-%d')})")
        
        dt = dt + timedelta(days=1)
    
    skip_reason = None
    if skip_reasons:
        skip_reason = f"Skipped: {', '.join(skip_reasons)}"
        logger.info(f"Schedule adjusted from {original_date.date()} to {dt.date()}. {skip_reason}")
    
    return dt, skip_reason


def add_business_days(dt: datetime, n: int) -> datetime:
    """
    Advance dt by exactly n US business days, preserving time-of-day and timezone.
    n=0 returns the next (or same) business day without advancing further.
    """
    if n == 0:
        result, _ = get_next_business_day(dt)
        return result
    added = 0
    current = dt
    while added < n:
        current = current + timedelta(days=1)
        if is_business_day(current):
            added += 1
    return current


def calculate_send_time(
    base_time: datetime,
    wait_days: int,
    state: Optional[str] = None,
    timezone_str: Optional[str] = None,
    send_hour: int = 9,
    send_minute: int = 0,
    add_jitter: bool = True
) -> datetime:
    """
    Calculate the exact send time for an email considering:
    - Wait days from base_time (counted as BUSINESS days)
    - Prospect's timezone (based on US state)
    - Skip weekends and US holidays
    - Optional jitter for anti-spam (1-5 minutes random offset)
    
    The email will be scheduled at [send_hour]:[send_minute] in the prospect's
    local timezone, then converted to UTC for storage.
    
    Args:
        base_time: Starting datetime (typically campaign activation time, in UTC)
        wait_days: Number of BUSINESS days to wait before sending (excludes weekends/holidays)
        state: Prospect's US state code or full name (e.g., "NY", "California")
        timezone_str: Explicit timezone (overrides state mapping if valid)
        send_hour: Hour to send email (0-23) in prospect's local time
        send_minute: Minute to send email (0-59) in prospect's local time
        add_jitter: If True, add random 0-5 minute offset to prevent spam detection
    
    Returns:
        datetime in UTC (naive, for database storage)
    """
    # Resolve timezone from state or explicit value
    tz_name = get_timezone_for_state(state=state, timezone=timezone_str)
    
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"Invalid timezone '{tz_name}', using default")
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    
    # Ensure base_time has UTC timezone
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=ZoneInfo("UTC"))
    
    # Convert to prospect's local timezone
    local_time = base_time.astimezone(tz)
    
    # Set the window start (9 AM) for today in the prospect's local time
    target_time = local_time.replace(
        hour=send_hour,
        minute=send_minute,
        second=0,
        microsecond=0
    )

    # Determine whether to send today vs tomorrow:
    # BEFORE window start (e.g. 7 AM, window opens 9 AM)  → send at 9 AM today
    # INSIDE the window  (e.g. 11:54 AM, window 09-17)    → send NOW (today!)
    # AFTER  window end  (e.g. 6 PM,  window closes 5 PM) → send at 9 AM tomorrow
    window_end_time = local_time.replace(
        hour=17,   # default end — callers using send_window_end should pass via timezone_str
        minute=0,
        second=0,
        microsecond=0
    )

    # Track the base minute for jitter application (changes per case below)
    base_minute = send_minute

    if local_time < target_time:
        # Before window opens — schedule at window start today; jitter from send_minute
        base_minute = send_minute
    elif local_time <= window_end_time:
        # Inside the window — schedule NOW so the email goes out today
        target_time = local_time.replace(second=0, microsecond=0)
        base_minute = local_time.minute   # jitter from current minute, not 0
    else:
        # Window has closed for today — push to 9 AM tomorrow
        target_time = target_time + timedelta(days=1)
        base_minute = send_minute

    # Skip to first business day if needed
    target_time, _ = get_next_business_day(target_time)

    # Now add BUSINESS days (not calendar days)
    business_days_added = 0
    while business_days_added < wait_days:
        target_time = target_time + timedelta(days=1)
        if is_business_day(target_time):
            business_days_added += 1

    # Calculate jitter (random 0-5 min offset to prevent all emails hitting at once)
    jitter_minutes = 0
    if add_jitter:
        jitter_minutes = random.randint(SEND_TIME_JITTER_MIN, SEND_TIME_JITTER_MAX)

    # Apply jitter safely using timedelta.
    # Using replace(minute=base_minute + jitter_minutes) can randomly raise:
    # ValueError: minute must be in 0..59 (e.g., minute=58 + jitter=5).
    target_time = target_time.replace(second=0, microsecond=0)
    target_time = target_time + timedelta(
        minutes=jitter_minutes,
        seconds=random.randint(0, 59)
    )
    
    # Convert back to UTC for database storage (naive datetime)
    utc_time = target_time.astimezone(ZoneInfo("UTC"))
    return utc_time.replace(tzinfo=None)


def calculate_send_time_with_window(
    base_time: datetime,
    wait_days: int,
    state: Optional[str] = None,
    timezone_str: Optional[str] = None,
    send_window_start: Optional[time] = None,
    send_window_end: Optional[time] = None,
    add_jitter: bool = True
) -> datetime:
    """
    Calculate send time using a send window instead of exact time.
    Email will be scheduled at the start of the window.
    
    Args:
        base_time: Starting datetime (typically campaign activation time, in UTC)
        wait_days: Number of days to wait before sending
        state: Prospect's US state code or full name
        timezone_str: Explicit timezone (overrides state mapping)
        send_window_start: Start of send window (default: 9:00 AM)
        send_window_end: End of send window (default: 6:00 PM)
        add_jitter: If True, add random 0-5 minute offset
    
    Returns:
        datetime in UTC (naive, for database storage)
    """
    if send_window_start is None:
        send_window_start = time(9, 0)
    
    return calculate_send_time(
        base_time=base_time,
        wait_days=wait_days,
        state=state,
        timezone_str=timezone_str,
        send_hour=send_window_start.hour,
        send_minute=send_window_start.minute,
        add_jitter=add_jitter
    )


def calculate_spread_send_time(
    send_window_start: time,
    timezone_str: str,
    base_date,
    total_prospects: int,
    prospect_index: int,
    send_window_end: Optional[time] = None,
    min_gap_minutes: int = 2,
) -> datetime:
    """
    Calculate UTC send time for a specific prospect by spreading all prospects
    evenly across the send window on a given date.

    If send_window_end is given: divides the window into equal slots so all
    prospects fit within (start, end) on the same business day — capped at
    min_gap_minutes apart, so a short list in a long window doesn't get
    stretched needlessly slowly just because the window is long (mirrors the
    same cap applied in schedule_email_in_window).
    If send_window_end is None: spaces prospects min_gap_minutes apart (or 2
    minutes if min_gap_minutes is unset) from start time.

    Rolls forward to the next business day if the computed slot lands on a
    weekend or holiday (only relevant when send_window_end is None and there
    are many prospects).

    Returns a naive UTC datetime suitable for database storage.
    """
    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(f"Invalid timezone '{timezone_str}', using default")
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    # Ensure base_date is a date object
    if isinstance(base_date, datetime):
        base_date = base_date.date()

    # Slot offset in minutes for this prospect
    if send_window_end and send_window_end > send_window_start:
        start_mins = send_window_start.hour * 60 + send_window_start.minute
        end_mins = send_window_end.hour * 60 + send_window_end.minute
        window_minutes = end_mins - start_mins
        natural_slot = window_minutes / max(total_prospects, 1)
        slot_minutes = min(natural_slot, max(min_gap_minutes, 0))
    else:
        # No end time — space min_gap_minutes apart (2 min if unset)
        slot_minutes = min_gap_minutes if min_gap_minutes > 0 else 2.0

    offset_minutes = prospect_index * slot_minutes

    # Build a local datetime at window_start on base_date
    scheduled_local = datetime(
        base_date.year, base_date.month, base_date.day,
        send_window_start.hour, send_window_start.minute, 0,
        tzinfo=tz,
    ) + timedelta(minutes=offset_minutes)

    # Skip non-business days (only matters if offset pushed past midnight)
    days_checked = 0
    while not is_business_day(scheduled_local) and days_checked < 14:
        next_day = scheduled_local.date() + timedelta(days=1)
        scheduled_local = datetime(
            next_day.year, next_day.month, next_day.day,
            send_window_start.hour, send_window_start.minute, 0,
            tzinfo=tz,
        )
        days_checked += 1

    # Convert to naive UTC
    utc_time = scheduled_local.astimezone(ZoneInfo("UTC"))
    return utc_time.replace(tzinfo=None)


def _coerce_time(value, fallback: time) -> time:
    """
    Coerce DB/time-window values to datetime.time safely.

    Handles:
    - datetime.time
    - datetime.timedelta (common for some raw SQL/MySQL adapters)
    - None / invalid values -> fallback
    """
    if value is None:
        return fallback
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds()) % 86400
        hour = total_seconds // 3600
        minute = (total_seconds % 3600) // 60
        second = total_seconds % 60
        return time(hour, minute, second)
    return fallback


def is_within_send_window(
    current_utc: datetime,
    timezone_str: str,
    send_window_start: time,
    send_window_end: time
) -> bool:
    """
    Check if the current UTC time falls within the allowed send window
    for a specific timezone on a valid business day.
    
    Args:
        current_utc: The current time in UTC
        timezone_str: Target timezone (e.g. 'America/New_York', 'UTC')
        send_window_start: Start of business hours (e.g. 09:00:00)
        send_window_end: End of business hours (e.g. 17:00:00)
        
    Returns:
        True if current local time is a business day and within business hours.
    """
    # --- None guards: apply safe defaults if DB stored NULL ---
    send_window_start = _coerce_time(send_window_start, time(9, 0))
    send_window_end = _coerce_time(send_window_end, time(17, 0))

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        logger.warning(f"Invalid timezone '{timezone_str}', falling back to UTC")
        tz = ZoneInfo("UTC")

    # Make sure we have a timezone-aware UTC datetime
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=ZoneInfo("UTC"))
        
    # Convert UTC time to Target Timezone
    local_time = current_utc.astimezone(tz)
    
    # Check if it is a valid business day in local time
    if not is_business_day(local_time):
        return False
        
    # Check time window
    local_time_only = local_time.time()
    
    return send_window_start <= local_time_only <= send_window_end


def schedule_email_in_window(
    base_utc: datetime,
    timezone_str: str,
    send_window_start: Optional[time],
    send_window_end: Optional[time],
    sending_mode: str = "spread",
    prospect_index: int = 0,
    total_prospects: int = 1,
    min_gap_minutes: int = 2,
    batch_size: Optional[int] = None,
    batch_gap_minutes: int = 30,
) -> datetime:
    """
    Compute the UTC send time for a single email according to the campaign's
    sending schedule.

    Modes
    -----
    spread  - Evenly distribute all prospects across the window.
              prospect_index=0 -> window start, last prospect -> window end.
    random  - Random time within window, respecting min_gap_minutes.
    batch   - Group prospects into batches of batch_size; each batch fires
              batch_gap_minutes after the previous one starts.

    When send_window_end is None, falls back to 2-minute spacing from start.

    Returns a naive UTC datetime suitable for EmailMessage.scheduled_at.
    """
    def _parse_time(t):
        if t is None:
            return None
        if isinstance(t, str):
            h, m = t.split(":")[:2]
            return time(int(h), int(m))
        return t

    def _align_origin(origin: datetime, now_local: datetime, step_minutes: int) -> datetime:
        """Align to the next valid slot from now."""
        anchor = now_local.replace(second=0, microsecond=0)
        if now_local.second > 0 or now_local.microsecond > 0:
            anchor += timedelta(minutes=1)
        if step_minutes > 1:
            elapsed = max(0, int((anchor - origin).total_seconds() // 60))
            rem = elapsed % step_minutes
            if rem:
                anchor += timedelta(minutes=(step_minutes - rem))
        return max(origin, anchor)

    send_window_start = _parse_time(send_window_start)
    send_window_end = _parse_time(send_window_end)

    if send_window_start is None and send_window_end is None:
        return base_utc.replace(tzinfo=None) if base_utc.tzinfo else base_utc

    send_window_start = send_window_start or time(9, 0)
    sending_mode = (sending_mode or "spread").lower()
    min_gap_minutes = max(int(min_gap_minutes or 0), 0)
    batch_gap_minutes = max(int(batch_gap_minutes or 30), 1)

    try:
        tz = ZoneInfo(timezone_str or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")

    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=ZoneInfo("UTC"))

    local_now = base_utc.astimezone(tz)

    window_end_for_check = send_window_end or time(23, 59)
    today_window_end = local_now.replace(
        hour=window_end_for_check.hour,
        minute=window_end_for_check.minute,
        second=0, microsecond=0,
    )
    if local_now <= today_window_end:
        base_date = local_now.date()
    else:
        base_date = (local_now + timedelta(days=1)).date()

    candidate = datetime(
        base_date.year, base_date.month, base_date.day,
        send_window_start.hour, send_window_start.minute,
        tzinfo=tz,
    )
    candidate, _ = get_next_business_day(candidate)
    base_date = candidate.date()

    window_start_local = datetime(
        base_date.year, base_date.month, base_date.day,
        send_window_start.hour, send_window_start.minute, 0,
        tzinfo=tz,
    )
    window_close_local = None
    if send_window_end:
        window_close_local = datetime(
            base_date.year, base_date.month, base_date.day,
            send_window_end.hour, send_window_end.minute, 0,
            tzinfo=tz,
        )

    # Catch-up: launch mid-window should start from next valid slot today.
    origin_local = window_start_local
    if base_date == local_now.date() and local_now > window_start_local:
        if window_close_local and local_now >= window_close_local:
            next_day = base_date + timedelta(days=1)
            next_candidate = datetime(
                next_day.year, next_day.month, next_day.day,
                send_window_start.hour, send_window_start.minute, 0,
                tzinfo=tz,
            )
            next_candidate, _ = get_next_business_day(next_candidate)
            base_date = next_candidate.date()
            origin_local = next_candidate
            if send_window_end:
                window_close_local = datetime(
                    base_date.year, base_date.month, base_date.day,
                    send_window_end.hour, send_window_end.minute, 0,
                    tzinfo=tz,
                )
        else:
            if sending_mode == "batch":
                # For batch mode, start from the next minute when launching
                # mid-window (do not snap to window-start grid boundaries).
                step_for_alignment = 1
            elif sending_mode == "random":
                step_for_alignment = max(min_gap_minutes, 1)
            else:
                step_for_alignment = 1
            origin_local = _align_origin(window_start_local, local_now, step_for_alignment)
            if window_close_local and origin_local > window_close_local:
                next_day = base_date + timedelta(days=1)
                next_candidate = datetime(
                    next_day.year, next_day.month, next_day.day,
                    send_window_start.hour, send_window_start.minute, 0,
                    tzinfo=tz,
                )
                next_candidate, _ = get_next_business_day(next_candidate)
                base_date = next_candidate.date()
                origin_local = next_candidate
                window_close_local = datetime(
                    base_date.year, base_date.month, base_date.day,
                    send_window_end.hour, send_window_end.minute, 0,
                    tzinfo=tz,
                ) if send_window_end else None

    if window_close_local and window_close_local > origin_local:
        window_minutes = int((window_close_local - origin_local).total_seconds() // 60)
    else:
        window_minutes = None

    if sending_mode == "spread":
        if window_minutes:
            natural_slot = window_minutes / max(total_prospects, 1)
            # Spreading strictly divides the remaining window by contact count,
            # which stretches a small/medium list across the *entire* window
            # even when nothing requires it (e.g. 250 contacts in a 14-hour
            # window spaces them ~3.4 min apart just because the window is
            # that long). Cap the gap at min_gap_minutes so a campaign only
            # takes as long as the window actually forces it to — if the list
            # is big enough that the natural per-window slot is already
            # tighter than min_gap_minutes, that denser pace is kept as-is
            # (list still fits the window, nothing changes there).
            slot = min(natural_slot, max(min_gap_minutes, 0))
            offset_minutes = prospect_index * slot
        else:
            offset_minutes = prospect_index * max(min_gap_minutes, 2)

    elif sending_mode == "batch":
        effective_batch = batch_size or max(total_prospects, 1)
        batch_index = prospect_index // effective_batch
        if window_minutes:
            max_offset = max(window_minutes - 1, 0)
            offset_minutes = min(batch_index * batch_gap_minutes, max_offset)
        else:
            offset_minutes = batch_index * batch_gap_minutes

    else:  # random
        if window_minutes:
            gap = max(min_gap_minutes, 1)
            slots = max(window_minutes // gap, 1)
            seed = (
                f"{base_date.isoformat()}|{timezone_str}|"
                f"{origin_local.hour:02d}:{origin_local.minute:02d}|"
                f"{(window_close_local.hour if window_close_local else 23):02d}:"
                f"{(window_close_local.minute if window_close_local else 59):02d}|"
                f"{total_prospects}|{gap}"
            )
            rng = random.Random(seed)
            if total_prospects <= slots:
                slot_map = rng.sample(range(int(slots)), k=max(int(total_prospects), 1))
                chosen_slot = slot_map[min(max(prospect_index, 0), len(slot_map) - 1)]
            else:
                chosen_slot = rng.randint(0, int(slots) - 1)
            offset_minutes = chosen_slot * gap
        else:
            offset_minutes = random.randint(0, 60)

    # Keep strict minute-based gap guarantees for random mode.
    # (Random slot selection already provides distribution variance.)
    jitter_seconds = 0
    send_local = origin_local + timedelta(minutes=offset_minutes, seconds=jitter_seconds)

    if window_close_local and send_local > window_close_local:
        next_day = base_date + timedelta(days=1)
        next_candidate = datetime(
            next_day.year, next_day.month, next_day.day,
            send_window_start.hour, send_window_start.minute, 0,
            tzinfo=tz,
        )
        next_candidate, _ = get_next_business_day(next_candidate)
        send_local = next_candidate

    if send_local <= local_now:
        send_local = _align_origin(window_start_local, local_now, 1)
        if window_close_local and send_local > window_close_local:
            next_day = base_date + timedelta(days=1)
            next_candidate = datetime(
                next_day.year, next_day.month, next_day.day,
                send_window_start.hour, send_window_start.minute, 0,
                tzinfo=tz,
            )
            next_candidate, _ = get_next_business_day(next_candidate)
            send_local = next_candidate

    utc_time = send_local.astimezone(ZoneInfo("UTC"))
    return utc_time.replace(tzinfo=None)
