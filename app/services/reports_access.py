"""
What the report services need from the rest of the CRM (ADR 0009, chunk A6), behind one Protocol so
the report services can be unit-tested with a fake: record visibility (contact_service), amount
visibility and masking (sales_settings), names and record dicts (sales), forecast snapshots and the
daily new-contact limit.

``SessionReportAccess`` is the production implementation over a request's session; the services
it delegates to still take that session.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional, Protocol

from app.repositories.reports import Session
from app.services import contact_service
from app.services import daily_limit as limits
from app.services import forecast_snapshots as fs
from app.services import sales as svc
from app.services import sales_settings


class ReportAccess(Protocol):
    def visible_user_ids(self, user: Any) -> Optional[set[str]]:
        """Owners whose records the user may see; None = everyone's."""
        ...

    def can_see_owner(self, user: Any, owner_id: Optional[str]) -> bool: ...
    def team_user_ids(self, user: Any) -> set[str]: ...
    def can_manage_contacts(self, user: Any) -> bool: ...
    def sees_everything(self, user: Any) -> bool: ...
    def can_see_amounts(self, user: Any) -> bool: ...
    def masked(self, user: Any, data: Any, keys: Optional[Iterable[str]] = None) -> Any: ...
    def mask_money(self, data: Any, keys: Iterable[str]) -> Any: ...
    def user_names(self, ids: Iterable[str]) -> dict[str, str]: ...
    def lead_dicts(self, leads: list[Any]) -> list[dict]: ...
    def opp_dicts(self, opps: list[Any]) -> list[dict]: ...
    def stage_probabilities(self, tenant_id: str) -> dict[str, int]: ...
    def daily_limit(self, user: Any) -> dict: ...
    def forecast_history(self, tenant_id: str, period_type: str, owners: Optional[set[str]], periods: int) -> list[dict]: ...
    def forecast_figures(self, tenant_id: str, start: date, owners: Optional[set[str]]) -> dict: ...
    def take_snapshot(self, tenant_id: str, period_type: str, taken_by: str, replace: bool) -> Any: ...


class SessionReportAccess:
    """``ReportAccess`` over a request's session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def visible_user_ids(self, user: Any) -> Optional[set[str]]:
        return contact_service.visible_user_ids(self._db, user)

    def can_see_owner(self, user: Any, owner_id: Optional[str]) -> bool:
        return contact_service.can_see_owner(self._db, user, owner_id)

    def team_user_ids(self, user: Any) -> set[str]:
        return set(contact_service.team_user_ids(self._db, user))

    def can_manage_contacts(self, user: Any) -> bool:
        return contact_service.can_manage_contacts(user)

    def sees_everything(self, user: Any) -> bool:
        return contact_service.sees_everything(user)

    def can_see_amounts(self, user: Any) -> bool:
        return sales_settings.can_see_amounts(self._db, user)

    def masked(self, user: Any, data: Any, keys: Optional[Iterable[str]] = None) -> Any:
        if keys is None:
            return sales_settings.masked_for(self._db, user, data)
        return sales_settings.masked_for(self._db, user, data, keys=keys)

    def mask_money(self, data: Any, keys: Iterable[str]) -> Any:
        return sales_settings.mask_money(data, keys)

    def user_names(self, ids: Iterable[str]) -> dict[str, str]:
        return svc.user_names(self._db, ids)

    def lead_dicts(self, leads: list[Any]) -> list[dict]:
        return svc.lead_dicts(self._db, leads)

    def opp_dicts(self, opps: list[Any]) -> list[dict]:
        return svc.opp_dicts(self._db, opps)

    def stage_probabilities(self, tenant_id: str) -> dict[str, int]:
        return {s.stage_id: s.probability for s in svc.stages(self._db, tenant_id, True)}

    def daily_limit(self, user: Any) -> dict:
        return limits.status_for(self._db, user)

    def forecast_history(self, tenant_id: str, period_type: str, owners: Optional[set[str]], periods: int) -> list[dict]:
        return fs.history(self._db, tenant_id, period_type, owners, periods)

    def forecast_figures(self, tenant_id: str, start: date, owners: Optional[set[str]]) -> dict:
        return fs.period_figures(self._db, tenant_id, "QUARTER", start, owners)

    def take_snapshot(self, tenant_id: str, period_type: str, taken_by: str, replace: bool) -> Any:
        return fs.take_snapshot(self._db, tenant_id, period_type, source="MANUAL", taken_by=taken_by, replace=replace)
