"""
The send calendar the campaign scheduling rules (app/domain/campaigns_edit.py) are given: US business
days and the send-window planner of app/utils/business_calendar.py behind the domain's SendCalendar
protocol, so the domain stays pure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.domain.campaigns_edit import ScheduleSettings
from app.utils import business_calendar


class BusinessCalendar:
    def add_business_days(self, moment: datetime, days: int) -> datetime:
        return business_calendar.add_business_days(moment, days)

    def batch_day_start(self, base: datetime, day_offset: int) -> datetime:
        return business_calendar.batch_day_start(base, day_offset)

    def timezone_for_state(self, state: str, default: str) -> Optional[str]:
        return business_calendar.get_timezone_for_state(state, default)

    def place_in_window(self, base: datetime, timezone: str, settings: ScheduleSettings, index: int,
                        total: int) -> datetime:
        return business_calendar.schedule_email_in_window(
            base_utc=base, timezone_str=timezone, send_window_start=settings.window_start,
            send_window_end=settings.window_end, sending_mode=settings.sending_mode, prospect_index=index,
            total_prospects=total, min_gap_minutes=settings.min_gap_minutes, batch_size=settings.batch_size,
            batch_gap_minutes=settings.batch_gap_minutes,
        )


CALENDAR = BusinessCalendar()


def schedule_settings(campaign: Any) -> ScheduleSettings:
    """The campaign's send settings with the business calendar."""
    return ScheduleSettings.of(campaign, CALENDAR)
