"""
Custom reports (BRD v2.0 BR-SF-14): managers pick an object, filters, a grouping (optionally a second one
for a matrix), a measure and a date range, save it, and share it. Every run is limited to the viewer's
records and their team's, so a shared report shows each person only what they may see.

Rules here; the catalogue and definition checks in app/domain/reports.py; SQL in
app/repositories/reports_custom.py (chunk A6 moved all of it out of the router).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from app.core.errors import BadRequestError, DomainError, NotFoundError, PermissionDenied, ValidationFailed
from app.domain.reports import (
    MONEY_MEASURES,
    DefinitionSpec,
    ReportInputError,
    ReportViewer,
    can_build_reports,
    catalogue,
    check_date_field,
    check_definition,
    check_filter,
    date_bounds,
    report_total,
    shape_flat,
    shape_matrix,
)
from app.models.sales import LEAD_STAGES
from app.repositories.base import SqlAlchemyUnitOfWork, UnitOfWork
from app.repositories.reports_custom import (
    CustomReportRepository,
    ReportOwnerScope,
    Session,
    SqlAlchemyCustomReportRepository,
)
from app.schemas.reports_custom_schema import Definition, ReportWrite
from app.services.contact_service import clean_str
from app.services.reports_access import ReportAccess, SessionReportAccess

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
STAGE_ID_LENGTH = 36   # deal stages are ids; lead stages are fixed codes


def _domain_error(exc: ReportInputError) -> DomainError:
    if exc.status == 422:
        return ValidationFailed(exc.message)
    if exc.status == 403:
        return PermissionDenied(exc.message)
    return BadRequestError(exc.message)


def _spec(d: Definition) -> DefinitionSpec:
    return DefinitionSpec(object=d.object, group_by=d.group_by, group_by_2=d.group_by_2, measure=d.measure,
                          filters=tuple(d.filters or []), date_field=d.date_field, date_from=d.date_from,
                          date_to=d.date_to)


class CustomReports:
    def __init__(self, repository: CustomReportRepository, access: ReportAccess,
                 uow: Optional[UnitOfWork] = None) -> None:
        self.repo = repository
        self.access = access
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "CustomReports":
        return cls(SqlAlchemyCustomReportRepository(db), SessionReportAccess(db), SqlAlchemyUnitOfWork.for_session(db))

    # ── catalogue and runs ──

    @staticmethod
    def can_build(user: ReportViewer) -> bool:
        return can_build_reports(user.role, user.sales_level)

    @staticmethod
    def meta(user: ReportViewer) -> dict:
        """GET /reports/custom/meta: the catalogue and whether the viewer may build reports."""
        return catalogue(can_build_reports(user.role, user.sales_level))

    def _require_builder(self, user: ReportViewer) -> None:
        if not self.can_build(user):
            raise PermissionDenied("Only managers can build reports")

    def _scope(self, user: ReportViewer) -> ReportOwnerScope:
        ids = self.access.visible_user_ids(user)
        if ids is None:
            return ReportOwnerScope()
        return ReportOwnerScope(frozenset(ids), self.access.can_manage_contacts(user))

    def _labels(self, key: Optional[str], values: Iterable[Any]) -> Dict[Any, str]:
        present = [v for v in values if v]
        if not present or not key:
            return {}
        if key == "owner":
            return self.access.user_names(present)
        if key == "stage":
            if len(str(present[0])) == STAGE_ID_LENGTH:
                return self.repo.stage_names(present)
            return {k: v[0] for k, v in LEAD_STAGES.items()}
        if key == "campaign":
            return self.repo.campaign_names(present)
        if key == "company":
            return self.repo.company_names(present)
        return {}

    def run_definition(self, user: ReportViewer, d: Definition) -> dict:
        """Validate the definition against the catalogue and run it in the viewer's scope."""
        try:
            return self._run(user, _spec(d))
        except ReportInputError as exc:
            raise _domain_error(exc) from None

    def _run(self, user: ReportViewer, d: DefinitionSpec) -> dict:
        obj = check_definition(d)
        money = d.measure in MONEY_MEASURES
        if money and not self.access.can_see_amounts(user):
            raise PermissionDenied("Your role cannot see amounts")
        kinds = self.repo.field_kinds(d.object)
        filters = [check_filter(obj.fields, kinds, f, user.user_id) for f in d.filters]
        lower = upper = None
        if d.date_field:
            check_date_field(obj, d.date_field)
            lower, upper = date_bounds(d.date_from, d.date_to)
        groups = [d.group_by] + ([d.group_by_2] if d.group_by_2 else [])
        rows = self.repo.run(user.tenant_id, d.object, self._scope(user), filters, d.date_field, lower, upper, groups,
                             d.measure)
        averaged = d.measure == "avg_amount"
        labels = self._labels(d.group_by, [r[0] for r in rows])
        if not d.group_by_2:
            out = shape_flat([(r[0], r[1]) for r in rows], labels, d.group_by)
            columns = None
        else:
            col_labels = self._labels(d.group_by_2, [r[1] for r in rows])
            out, columns = shape_matrix([(r[0], r[1], r[2]) for r in rows], labels, col_labels, averaged)
        return {"rows": out, "columns": columns, "total": report_total(out, averaged), "money": money,
                "measure_label": obj.measures[d.measure]}

    def run_adhoc(self, user: ReportViewer, d: Definition) -> dict:
        """Ad-hoc run while building: same permission as saving a report (shared reports run via GET /{id})."""
        self._require_builder(user)
        return self.run_definition(user, d)

    # ── saved reports ──

    @staticmethod
    def report_dict(r: Any, user: ReportViewer, names: Optional[dict] = None) -> dict:
        return {"report_id": r.report_id, "name": r.name, "description": r.description, "definition": r.definition,
                "shared": r.shared, "owner_id": r.owner_id, "owner_name": (names or {}).get(r.owner_id),
                "can_edit": r.owner_id == user.user_id or user.role in ADMIN_ROLES, "updated_at": r.updated_at}

    def list_reports(self, user: ReportViewer) -> list:
        rows = self.repo.visible_reports(user.tenant_id, user.user_id)
        names = self.access.user_names([r.owner_id for r in rows])
        return [self.report_dict(r, user, names) for r in rows]

    def _get(self, user: ReportViewer, report_id: str, edit: bool = False) -> Any:
        r = self.repo.get(user.tenant_id, report_id)
        if not r or not (r.shared or r.owner_id == user.user_id):
            raise NotFoundError("Report not found")
        if edit and not self.report_dict(r, user)["can_edit"]:
            raise PermissionDenied("Only the report's owner or an admin can change it")
        return r

    def _uow(self) -> UnitOfWork:
        if self.uow is None:
            raise RuntimeError("saving reports needs a unit of work")
        return self.uow

    def create(self, user: ReportViewer, payload: ReportWrite) -> dict:
        self._require_builder(user)
        if not clean_str(payload.name) or not payload.definition:
            raise BadRequestError("Give the report a name and a definition")
        self.run_definition(user, payload.definition)  # validates
        uow = self._uow()
        with uow:
            r = self.repo.add(self.repo.new_report(
                tenant_id=user.tenant_id, owner_id=user.user_id, name=clean_str(payload.name),
                description=payload.description, definition=payload.definition.model_dump(mode="json"),
                shared=bool(payload.shared)))
            uow.commit()
            return self.report_dict(r, user)

    def run_saved(self, user: ReportViewer, report_id: str) -> dict:
        r = self._get(user, report_id)
        return {**self.report_dict(r, user), "result": self.run_definition(user, Definition(**r.definition))}

    def update(self, user: ReportViewer, report_id: str, payload: ReportWrite) -> dict:
        r = self._get(user, report_id, edit=True)
        if payload.definition:
            self.run_definition(user, payload.definition)
        uow = self._uow()
        with uow:
            if payload.definition:
                r.definition = payload.definition.model_dump(mode="json")
            if payload.name is not None:
                r.name = clean_str(payload.name) or r.name
            if payload.description is not None:
                r.description = payload.description
            if payload.shared is not None:
                r.shared = payload.shared
            uow.commit()
            return self.report_dict(r, user)

    def delete(self, user: ReportViewer, report_id: str) -> dict:
        r = self._get(user, report_id, edit=True)
        uow = self._uow()
        with uow:
            self.repo.delete(r)
            uow.commit()
        return {"status": "deleted", "report_id": report_id}
