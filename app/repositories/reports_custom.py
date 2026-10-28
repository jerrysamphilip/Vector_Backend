"""
Custom report builder repository (BRD v2.0 BR-SF-14; chunk A6): the grouped query a report definition
runs, the labels of its group keys, and the saved reports.

Safe SQL by construction: the request only ever names catalogue keys (``app.domain.reports.CUSTOM_OBJECTS``,
validated by the service); this module maps each key to a fixed ORM column below. No identifier or SQL
fragment is built from request text, and filter values are bound parameters. Every run is limited to
the tenant and to the owners in the ``ReportOwnerScope`` the service computed for the viewer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import ColumnElement, Date, DateTime, Integer, Numeric, and_, func, or_
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Session

from app.domain.reports import FieldKind, FilterSpec
from app.models.account import Account
from app.models.campaign import Campaign
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, SalesStage
from app.models.sales_extra import SavedReport
from app.repositories.base import TenantId
from app.utils.sql_like import ilike

__all__ = ["CustomReportRepository", "ReportOwnerScope", "Session", "SqlAlchemyCustomReportRepository",
           "filter_condition", "value_kind"]


def _month(col: Any) -> Any:
    return func.date_format(col, "%Y-%m")


@dataclass(frozen=True)
class _Source:
    model: Any
    owner: Any
    fields: dict[str, Any]
    dates: dict[str, Any]
    amount: Any = None


# Catalogue key -> column. Keys match app.domain.reports.CUSTOM_OBJECTS exactly.
SOURCES: dict[str, _Source] = {
    "leads": _Source(
        Lead, Lead.owner_id,
        {"stage": Lead.stage, "owner": Lead.owner_id, "source": Lead.source, "campaign": Lead.campaign_id,
         "created_month": _month(Lead.created_at), "disqualified_reason": Lead.disqualified_reason},
        {"created_at": Lead.created_at, "qualified_at": Lead.qualified_at, "converted_at": Lead.converted_at}),
    "deals": _Source(
        Opportunity, Opportunity.owner_id,
        {"stage": Opportunity.stage_id, "status": Opportunity.status, "owner": Opportunity.owner_id,
         "client_type": Opportunity.client_type, "forecast_category": Opportunity.forecast_category,
         "campaign": Opportunity.campaign_id, "company": Opportunity.account_id,
         "close_month": _month(Opportunity.close_date), "created_month": _month(Opportunity.created_at),
         "closed_reason": Opportunity.closed_reason},
        {"created_at": Opportunity.created_at, "close_date": Opportunity.close_date,
         "closed_at": Opportunity.closed_at},
        Opportunity.amount),
    "contacts": _Source(
        Prospect, Prospect.owner_id,
        {"lifecycle_stage": Prospect.lifecycle_stage, "lead_status": Prospect.lead_status,
         "owner": Prospect.owner_id, "country": Prospect.poc_country, "industry": Prospect.industry,
         "lead_source": Prospect.lead_source, "consent_status": Prospect.consent_status,
         "created_month": _month(Prospect.created_at)},
        {"created_at": Prospect.created_at}),
    "activities": _Source(
        ContactActivity, ContactActivity.created_by,
        {"activity_type": ContactActivity.activity_type, "owner": ContactActivity.created_by,
         "source": ContactActivity.source, "month": _month(ContactActivity.occurred_at)},
        {"occurred_at": ContactActivity.occurred_at}),
    "tasks": _Source(
        CrmTask, CrmTask.owner_id,
        {"status": CrmTask.status, "owner": CrmTask.owner_id, "priority": CrmTask.priority,
         "task_type": CrmTask.task_type},
        {"created_at": CrmTask.created_at, "due_at": CrmTask.due_at}),
}


@dataclass(frozen=True)
class ReportOwnerScope:
    """Owners whose records a run covers; None = everyone. ``include_unassigned`` adds records without
    an owner (users who manage contacts pick those up)."""

    owner_ids: Optional[frozenset[str]] = None
    include_unassigned: bool = False


def value_kind(col: Any) -> FieldKind:
    """numeric / date / enum / string, from the column's SQL type (month buckets are date_format text: string)."""
    col_type = getattr(col, "type", None)
    if isinstance(col_type, SAEnum) and col_type.enums:
        return FieldKind("enum", tuple(col_type.enums))
    if isinstance(col_type, (Integer, Numeric)):
        return FieldKind("numeric")
    if isinstance(col_type, (Date, DateTime)):
        return FieldKind("date")
    return FieldKind("string")


def filter_condition(col: Any, f: FilterSpec) -> ColumnElement[bool]:
    """A validated filter as a SQL condition on its whitelisted column; values are bound parameters."""
    if f.operator == "is_empty":
        return or_(col.is_(None), col == "")
    if f.operator == "is_not_empty":
        return and_(col.isnot(None), col != "")
    if f.operator == "contains":
        return ilike(col, f.values[0])
    match = col.in_(list(f.values)) if f.many else col == f.values[0]
    return match if f.operator == "is" else or_(col.is_(None), ~match)


def _owner_condition(column: Any, scope: ReportOwnerScope) -> Optional[ColumnElement[bool]]:
    if scope.owner_ids is None:
        return None
    clause: ColumnElement[bool] = column.in_(scope.owner_ids)
    return or_(clause, column.is_(None)) if scope.include_unassigned else clause


class CustomReportRepository(Protocol):
    def field_kinds(self, obj: str) -> dict[str, FieldKind]: ...
    def run(self, tenant_id: TenantId, obj: str, scope: ReportOwnerScope, filters: Sequence[FilterSpec],
            date_field: Optional[str], lower: Optional[datetime], upper: Optional[datetime],
            group_by: Sequence[str], measure: str) -> list[tuple]: ...
    def stage_names(self, stage_ids: Iterable[Any]) -> dict[Any, str]: ...
    def campaign_names(self, campaign_ids: Iterable[Any]) -> dict[Any, str]: ...
    def company_names(self, account_ids: Iterable[Any]) -> dict[Any, str]: ...
    def visible_reports(self, tenant_id: TenantId, user_id: str) -> list[Any]: ...
    def get(self, tenant_id: TenantId, report_id: str) -> Optional[Any]: ...
    def add(self, report: Any) -> Any: ...
    def delete(self, report: Any) -> None: ...
    def new_report(self, **values: Any) -> Any: ...


class SqlAlchemyCustomReportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def field_kinds(self, obj: str) -> dict[str, FieldKind]:
        return {name: value_kind(col) for name, col in SOURCES[obj].fields.items()}

    def run(self, tenant_id: TenantId, obj: str, scope: ReportOwnerScope, filters: Sequence[FilterSpec],
            date_field: Optional[str], lower: Optional[datetime], upper: Optional[datetime],
            group_by: Sequence[str], measure: str) -> list[tuple]:
        """(group key[, second group key], measure) rows."""
        src = SOURCES[obj]
        query = self._session.query(src.model).filter(src.model.tenant_id == tenant_id)
        if src.model is Prospect:
            query = query.filter(Prospect.deleted_at.is_(None))
        owner = _owner_condition(src.owner, scope)
        if owner is not None:
            query = query.filter(owner)
        for f in filters:
            query = query.filter(filter_condition(src.fields[f.field], f))
        if date_field:
            dcol = src.dates[date_field]
            if lower:
                query = query.filter(dcol >= lower)
            if upper:
                query = query.filter(dcol < upper)
        cols = [src.fields[g] for g in group_by]
        agg = {"count": func.count(), "sum_amount": func.coalesce(func.sum(src.amount), 0),
               "avg_amount": func.avg(src.amount)}[measure]
        return [tuple(r) for r in query.with_entities(*cols, agg).group_by(*cols).all()]

    def stage_names(self, stage_ids: Iterable[Any]) -> dict[Any, str]:
        return {s.stage_id: s.name for s in self._session.query(SalesStage).filter(SalesStage.stage_id.in_(list(stage_ids)))}

    def campaign_names(self, campaign_ids: Iterable[Any]) -> dict[Any, str]:
        return {c.campaign_id: c.campaign_name for c in self._session.query(
            Campaign.campaign_id, Campaign.campaign_name).filter(Campaign.campaign_id.in_(list(campaign_ids)))}

    def company_names(self, account_ids: Iterable[Any]) -> dict[Any, str]:
        return {a.account_id: a.name for a in self._session.query(Account.account_id, Account.name).filter(
            Account.account_id.in_(list(account_ids)))}

    def visible_reports(self, tenant_id: TenantId, user_id: str) -> list[Any]:
        """The tenant's shared reports and the user's own, by name."""
        return self._session.query(SavedReport).filter(
            SavedReport.tenant_id == tenant_id, or_(SavedReport.shared.is_(True), SavedReport.owner_id == user_id)
        ).order_by(SavedReport.name).all()

    def get(self, tenant_id: TenantId, report_id: str) -> Optional[Any]:
        return self._session.query(SavedReport).filter(SavedReport.report_id == report_id,
                                                       SavedReport.tenant_id == tenant_id).first()

    def new_report(self, **values: Any) -> Any:
        return SavedReport(**values)

    def add(self, report: Any) -> Any:
        self._session.add(report)
        return report

    def delete(self, report: Any) -> None:
        self._session.delete(report)
