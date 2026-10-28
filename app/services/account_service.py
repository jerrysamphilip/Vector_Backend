"""
Companies (BR-CM-03/04): list, record page, create / edit / soft delete / restore, custom properties (C-16),
field propagation to contacts (C-20) and export (C-07).

Each company has a unique domain per workspace; contacts are associated automatically from their email domain,
or by company name for personal email addresses. Editing a company's name, industry or size copies the change
to the contacts whose primary company it is, with a history row on each contact. Deleting is a soft delete: the
company is hidden (contacts keep their link) and can be restored for 90 days.

Layered (ADR 0009, chunk A1): ``AccountService`` serves one route per method in its own unit of work; SQL is in
app/repositories/accounts.py; pure rules and query objects in app/domain/accounts.py. Helpers shared with the
contacts aggregate (visibility, property history, amount masking) still take a session: they get ``uow.session``.
"""

import csv
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from openpyxl import Workbook

from app.core.clock import utcnow
from app.core.concurrency import claim_version, resolve_expected
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.accounts import (
    ACCOUNT_COLUMNS,
    EDITABLE,
    MONEY_COLUMNS,
    PROPAGATED_FIELDS,
    AccountCriteria,
    AccountFilter,
    name_from_domain,
    owner_criteria,
    parse_company_filter,
    propagation_values,
)
from app.domain.contacts import FilterError, parse_filter_param
from app.domain.paging import Paging, Sort
from app.models.account import Account
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.accounts import AccountRepository, AccountsAccess, AccountsUnitOfWork, Session, accounts_uow
from app.schemas.account_schema import AccountUpdate, AccountWrite
from app.services import contact_companies, crm
from app.services.contact_field_service import (
    COMPANY,
    clean_custom_fields,
    definitions_in_order,
    field_dict,
    label_map,
    required_missing,
)
from app.services.contact_query_service import EXPORT_ROW_LIMIT, ExportFile, can_export
from app.services.contact_query_service import history_items as _history_items
from app.services.contact_query_service import summaries as _summaries
from app.services.contact_query_service import user_name as _user_name
from app.services.contact_service import (
    can_manage_contacts,
    can_see_account,
    can_see_owner,
    clean_str,
    require_manager,
    sees_everything,
    visibility_scope,
)
from app.services.export_log import record_export
from app.services.sales_settings import can_see_amounts, masked_for
from app.utils.company_domain import is_valid_domain

__all__ = ["AccountService", "account_dict", "can_edit_account", "propagate_to_contacts"]

EXPORT_CHUNK = 1000
DEFAULT_COLUMNS = ("name", "industry", "city", "lifecycle_stage", "annual_revenue", "contact_count", "owner_name",
                   "updated_at")


# ── Serialisation and rules ────────────────────────────────────────

def account_dict(a: Account, owner: Optional[User] = None, contact_count: int = 0) -> dict:
    return {
        "account_id": a.account_id, "row_version": a.row_version, "name": a.name, "domain": a.domain,
        "website": a.website,
        "phone": a.phone, "industry": a.industry, "emp_band": a.emp_band, "street": a.street,
        "city": a.city, "state": a.state, "postal_code": a.postal_code, "country": a.country,
        "description": a.description, "annual_revenue": float(a.annual_revenue) if a.annual_revenue is not None else None,
        "lifecycle_stage": a.lifecycle_stage,
        "owner_id": a.owner_id, "owner_name": _user_name(owner), "contact_count": contact_count,
        "created_at": a.created_at, "updated_at": a.updated_at,
        "custom_fields": dict(a.custom_fields or {}),
    }


def _archived_dict(a: Account, owner: Optional[User] = None, contact_count: int = 0) -> dict:
    deleted_at = a.deleted_at or utcnow()
    return {**account_dict(a, owner, contact_count), "deleted_at": a.deleted_at,
            "purge_at": deleted_at + timedelta(days=crm.RESTORE_WINDOW_DAYS)}


def can_edit_account(db: Session, user: User, account: Account) -> bool:
    if not can_see_account(db, user, account):
        return False
    return (can_manage_contacts(user) and (sees_everything(user) or account.owner_id is None
                                           or can_see_owner(db, user, account.owner_id))) or (
        account.owner_id is not None and can_see_owner(db, user, account.owner_id))


def website_domain(website: Optional[str]) -> Optional[str]:
    """The company domain a website implies; None when it is not a usable company domain."""
    domain = crm.clean_domain(website)
    return domain if domain and is_valid_domain(domain) and not crm.is_personal_domain(domain) else None


def _checked_domain(repo: AccountRepository, user: User, account: Account, raw: Optional[str]) -> Optional[str]:
    """A company domain as stored: valid syntax, not a free-mail provider, unique in the workspace."""
    domain = crm.clean_domain(raw)
    if not domain:
        return None
    if not is_valid_domain(domain):
        raise BadRequestError(f"'{raw}' is not a valid domain (for example acme.com).")
    if crm.is_personal_domain(domain):
        raise BadRequestError(f"{domain} is a personal email provider, not a company domain.")
    if repo.domain_taken(user.tenant_id, domain, except_id=account.account_id):
        raise ConflictError(f"Another company already has the domain {domain}.")
    return domain


def _check_owner(ctx: AccountsAccess, user: User, owner_id: Optional[str]) -> None:
    if not owner_id:
        return
    if not ctx.accounts.user_in_tenant(user.tenant_id, owner_id):
        raise BadRequestError("Owner must be a user in your workspace")
    if not can_see_owner(ctx.session, user, owner_id):
        raise PermissionDenied("You can only assign records to yourself or your team")


def _revenue(data: dict, allowed: bool) -> None:
    if "annual_revenue" not in data:
        return
    if not allowed:
        if data.pop("annual_revenue") not in (None, ""):  # a blank from a masked form leaves it as is
            raise PermissionDenied("Your role cannot see or change amounts")
        return
    if data["annual_revenue"] not in (None, ""):
        try:
            data["annual_revenue"] = Decimal(str(data["annual_revenue"]).replace(",", "").strip())
        except InvalidOperation:
            raise BadRequestError("Annual revenue must be a number") from None


def _check_lifecycle(user: User, account: Account, data: dict) -> None:
    if data.get("lifecycle_stage") and data["lifecycle_stage"] not in crm.LIFECYCLE_STAGES:
        raise BadRequestError(f"lifecycle_stage must be one of {list(crm.LIFECYCLE_STAGES)}")
    if "lifecycle_stage" in data and not crm.lifecycle_move_allowed(account.lifecycle_stage, data["lifecycle_stage"], user):
        raise BadRequestError("Lifecycle stage moves forward only. Ask an admin to change it.")


def apply_fields(ctx: AccountsAccess, user: User, account: Account, data: dict) -> None:
    """Validate and set the editable fields present in ``data`` (and the custom properties)."""
    if "owner_id" in data:
        _check_owner(ctx, user, data["owner_id"])
    _revenue(data, can_see_amounts(ctx.session, user))
    if "domain" in data:
        data["domain"] = _checked_domain(ctx.accounts, user, account, data["domain"])
    _check_lifecycle(user, account, data)
    for name in EDITABLE:
        if name in data:
            value = data[name]
            setattr(account, name, clean_str(value) if isinstance(value, str) and name != "description" else (value or None))
    if data.get("custom_fields") is not None:
        cleaned = clean_custom_fields(ctx.session, user.tenant_id, data["custom_fields"], COMPANY)
        merged = {**(account.custom_fields or {}), **cleaned}
        account.custom_fields = {k: v for k, v in merged.items() if v is not None} or None


def propagate_to_contacts(repo: AccountRepository, account: Account, before: dict, after: dict,
                          user_id: Optional[str]) -> int:
    """Copy name / industry / size changes to the contacts whose primary company it is (C-20), with one
    history row per changed contact field (source COMPANY). Returns the number of fields changed."""
    changes = {f: (before.get(f), after.get(f)) for f in PROPAGATED_FIELDS if before.get(f) != after.get(f)}
    if not changes:
        return 0
    changed = 0
    for prospect in repo.primary_contacts(account.tenant_id, account.account_id):
        touched = False
        for company_field, (old, new) in changes.items():
            contact_field = PROPAGATED_FIELDS[company_field]
            current = getattr(prospect, contact_field)
            change, value = propagation_values(company_field, old, new, current)
            if not change:
                continue
            setattr(prospect, contact_field, value)
            repo.add(account.tenant_id, PropertyChange(
                tenant_id=account.tenant_id, object_type="CONTACT", object_id=prospect.prospect_id,
                field=contact_field, old_value=current, new_value=value, source="COMPANY", changed_by=user_id))
            changed += 1
            touched = True
        if touched:
            prospect.row_version = Prospect.row_version + 1  # open contact forms see the change (T-3)
    return changed


def _company_history(ctx: AccountsAccess, user: User, rows: List[PropertyChange]) -> List[dict]:
    """Company history; custom property rows get the company property's label."""
    items = _history_items(ctx.session, user, rows)
    labels = label_map(ctx.session, user.tenant_id, COMPANY)
    for item in items:
        key = str(item.get("field") or "")
        if key.startswith("custom.") and key[len("custom."):] in labels:
            item["label"] = labels[key[len("custom."):]]
    return items


def _export_cell(key: str, value: Any) -> Any:
    if key == "lifecycle_stage":
        return crm.LIFECYCLE_STAGES.get(value, value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return value


def export_columns(columns: Optional[str], amounts: bool) -> List[str]:
    """Requested known column keys in order (all when none are valid); money columns only for those who may
    see amounts."""
    keys = [c for c in (columns or "").split(",") if c in ACCOUNT_COLUMNS] or list(ACCOUNT_COLUMNS)
    return [k for k in keys if amounts or k not in MONEY_COLUMNS]


def _filter_summary(f: AccountFilter, keys: List[str]) -> str:
    """What the export log keeps about the view (never the rows)."""
    parts = {"status": f.status, "q": f.q, "owner": f.owner, "lifecycle_stage": f.lifecycle_stage,
             "industry": f.industry, "filters": f.filters, "columns": ",".join(keys)}
    return json.dumps({k: v for k, v in parts.items() if v}, ensure_ascii=False, sort_keys=True)


def _write_file(fmt: str, header: List[Any], rows: List[List[Any]]) -> ExportFile:
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        if ws is None:  # a new workbook always has one sheet
            ws = wb.create_sheet()
        ws.title = "Companies"
        ws.append(crm.csv_safe_row(header))
        for r in rows:
            ws.append(r)
        buffer = io.BytesIO()
        wb.save(buffer)
        return ExportFile(buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          f"companies-{stamp}.xlsx")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(crm.csv_safe_row(header))
    writer.writerows(rows)
    return ExportFile(out.getvalue().encode("utf-8-sig"), "text/csv", f"companies-{stamp}.csv")


# ── Service ────────────────────────────────────────────────────────

class AccountService:
    """Companies for one request; each public method is one route and one unit of work."""

    def __init__(self, uow: AccountsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "AccountService":
        return cls(accounts_uow(db))

    # criteria ----------------------------------------------------------------
    @staticmethod
    def criteria(ctx: AccountsAccess, user: User, f: AccountFilter) -> AccountCriteria:
        archived = f.status == "archived"
        if archived:
            require_manager(user)
        try:
            tree = parse_company_filter(parse_filter_param(f.filters), user.user_id,
                                        lambda: can_see_amounts(ctx.session, user))
        except FilterError as exc:
            raise BadRequestError(str(exc)) from None
        owner_id, unassigned = owner_criteria(f.owner, user.user_id)
        return AccountCriteria(
            tenant_id=user.tenant_id, scope=visibility_scope(ctx.session, user), owner_id=owner_id,
            unassigned=unassigned, lifecycle_stage=f.lifecycle_stage, industry=f.industry,
            q=f.q.strip() if f.q and f.q.strip() else None, tree=tree, archived=archived,
            cutoff=utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS) if archived else None)

    def _find(self, ctx: AccountsAccess, user: User, account_id: str) -> Account:
        account = ctx.accounts.live(user.tenant_id, account_id)
        if not account or not can_see_account(ctx.session, user, account):
            raise NotFoundError("Company not found")
        return account

    # reads -------------------------------------------------------------------
    def list_page(self, user: User, f: AccountFilter, sort: Sort, paging: Paging) -> dict:
        with self.uow as uow:
            if sort.field == "annual_revenue" and not can_see_amounts(uow.session, user):
                sort = Sort("name", sort.direction)  # sorting by a hidden amount would reveal it (BR-SF-12)
            criteria = self.criteria(uow, user, f)
            page = uow.accounts.search(criteria, sort, paging)
            owners = uow.accounts.users_by_ids({a.owner_id for a, _ in page.items if a.owner_id})
            serialise = _archived_dict if criteria.archived else account_dict
            items = [serialise(a, owners.get(a.owner_id or ""), n) for a, n in page.items]
            # The company custom properties ride along, so the table can label their columns (C-16)
            properties = [field_dict(d) for d in definitions_in_order(uow.session, user.tenant_id, COMPANY)]
            return masked_for(uow.session, user, {"items": items, "total": page.total, "page": paging.page,
                                                  "page_size": paging.page_size, "custom_properties": properties,
                                                  "can_export": can_export(user)})

    def meta(self, user: User) -> dict:
        """Columns for the table / export and the company custom properties."""
        with self.uow as uow:
            amounts = can_see_amounts(uow.session, user)
            fields = [field_dict(d) for d in definitions_in_order(uow.session, user.tenant_id, COMPANY)]
        columns: List[Dict[str, Any]] = [{"key": k, "label": v} for k, v in ACCOUNT_COLUMNS.items()
                                         if amounts or k not in MONEY_COLUMNS]
        columns += [{"key": f"custom.{d['field_key']}", "label": d["label"], "custom": True} for d in fields]
        return {"columns": columns, "default_columns": [c for c in DEFAULT_COLUMNS if amounts or c not in MONEY_COLUMNS],
                "custom_fields": fields, "can_export": can_export(user), "can_manage": can_manage_contacts(user)}

    def get(self, user: User, account_id: str) -> dict:
        with self.uow as uow:
            return self._record(uow, user, account_id)

    def _record(self, ctx: AccountsAccess, user: User, account_id: str) -> dict:
        repo = ctx.accounts
        account = repo.live(user.tenant_id, account_id)
        if not account:
            raise NotFoundError("Company not found")
        contacts = repo.company_contacts(user.tenant_id, account_id, visibility_scope(ctx.session, user), 500)
        if not contacts and not can_see_account(ctx.session, user, account):
            raise NotFoundError("Company not found")
        contact_ids = [c.prospect_id for c in contacts]
        emails_sent, replies = repo.email_counts(contact_ids)
        owner = repo.users_by_ids({account.owner_id}).get(account.owner_id) if account.owner_id else None
        result = account_dict(account, owner, len(contacts))
        result["contacts"] = _summaries(ctx.session, contacts)
        labels = contact_companies.labels_for_company(ctx.session, account_id, contact_ids)  # CM-014
        for c in result["contacts"]:
            c["association_label"] = labels.get(c["prospect_id"])
            c["is_primary_company"] = c["account_id"] == account_id
        result["stats"] = {"contacts": len(contacts), "emails_sent": emails_sent, "replies": replies,
                           "open_tasks": repo.open_tasks(account_id)}
        result["can_edit"] = can_edit_account(ctx.session, user, account)
        result["can_delete"] = (can_manage_contacts(user) and user.role != "AGENT" and result["can_edit"])
        result["history"] = _company_history(ctx, user, repo.history(account_id, 200))
        result["custom_properties"] = [field_dict(d) for d in definitions_in_order(ctx.session, user.tenant_id, COMPANY)]
        return masked_for(ctx.session, user, result)

    def deleted(self, user: User) -> dict:
        require_manager(user)
        with self.uow as uow:
            cutoff = utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS)
            rows = [a for a in uow.accounts.deleted_since(user.tenant_id, cutoff)
                    if can_see_account(uow.session, user, a)]
            return masked_for(uow.session, user, {"items": [_archived_dict(a) for a in rows]})

    def export(self, user: User, fmt: str, columns: Optional[str], f: AccountFilter) -> ExportFile:
        """A company view as CSV / XLSX (C-07): same right as the contact export, formula-guarded, and refused
        (not cut) when it holds more rows than an export can."""
        if not can_export(user):
            raise PermissionDenied("Only admins and managers can export companies")
        with self.uow as uow:
            criteria = self.criteria(uow, user, f)
            total = uow.accounts.count(criteria)
            if total > EXPORT_ROW_LIMIT:
                raise BadRequestError(
                    f"This view has {total:,} companies; an export holds at most {EXPORT_ROW_LIMIT:,}. "
                    "Narrow the filters (owner, stage, industry or search) and export again.")
            keys = export_columns(columns, can_see_amounts(uow.session, user))
            defs = definitions_in_order(uow.session, user.tenant_id, COMPANY)
            header: List[Any] = [ACCOUNT_COLUMNS[k] for k in keys] + [d.label for d in defs]
            rows: List[List[Any]] = []
            for offset in range(0, total, EXPORT_CHUNK):
                chunk = uow.accounts.export_chunk(criteria, offset, EXPORT_CHUNK)
                owners = uow.accounts.users_by_ids({a.owner_id for a, _ in chunk if a.owner_id})
                for a, n in chunk:
                    row = account_dict(a, owners.get(a.owner_id or ""), n)
                    values = [_export_cell(k, row.get(k)) for k in keys]
                    values += [_export_cell("custom", (a.custom_fields or {}).get(d.field_key)) for d in defs]
                    rows.append(crm.csv_safe_row(values))  # no formula injection in Excel / Sheets
            export = _write_file(fmt, header, rows)
            record_export(uow, user.tenant_id, user.user_id, "COMPANY", _filter_summary(f, keys), len(rows), fmt)
            uow.commit()
            return export

    # writes ------------------------------------------------------------------
    def create(self, user: User, payload: AccountWrite) -> dict:
        data = payload.model_dump(exclude_unset=True)
        name = clean_str(data.get("name"))
        # An explicit domain is checked in apply_fields (400 when invalid or free mail); one taken from the
        # website is used only when it is a usable company domain
        domain = crm.clean_domain(data["domain"]) if data.get("domain") else website_domain(data.get("website"))
        if not name and domain:
            name = name_from_domain(domain)
        if not name:
            raise BadRequestError("Company name or domain is required")
        with self.uow as uow:
            if uow.accounts.name_taken(user.tenant_id, name):
                raise ConflictError("A company with this name already exists.")
            if data.get("owner_id") not in (None, user.user_id) and not can_manage_contacts(user):
                raise PermissionDenied("Only users who manage prospects can assign companies to others")
            account = Account(tenant_id=user.tenant_id, owner_id=user.user_id, lifecycle_stage="LEAD")
            data["name"] = name
            if domain:
                data["domain"] = domain
            apply_fields(uow, user, account, data)
            missing = required_missing(uow.session, user.tenant_id, account, COMPANY)
            if missing:
                raise BadRequestError(f"Required: {', '.join(missing)}")
            uow.accounts.add(user.tenant_id, account)
            uow.accounts.flush()
            uow.accounts.add(user.tenant_id, PropertyChange(
                tenant_id=user.tenant_id, object_type="COMPANY", object_id=account.account_id, field="created",
                new_value="Company created", source="UI", changed_by=user.user_id))
            uow.commit()
            owner = user if account.owner_id == user.user_id else None
            return masked_for(uow.session, user, account_dict(account, owner))

    def update(self, user: User, account_id: str, payload: AccountUpdate, if_match: Optional[int]) -> dict:
        with self.uow as uow:
            account = self._find(uow, user, account_id)
            if not can_edit_account(uow.session, user, account):
                raise PermissionDenied("You can only edit companies you own")
            data = payload.model_dump(exclude_unset=True)
            claim_version(uow.session, account, resolve_expected(data.pop("expected_version", None), if_match),
                          record="company")
            if "owner_id" in data and not can_manage_contacts(user):
                raise PermissionDenied("Only users who manage prospects can reassign companies")
            fields = EDITABLE + ("custom_fields",)
            before = crm.snapshot(account, fields)
            if "name" in data:
                data["name"] = self._new_name(uow.accounts, user, account, data["name"])
            apply_fields(uow, user, account, data)
            after = crm.snapshot(account, fields)
            crm.record_changes(uow.session, user.tenant_id, "COMPANY", account_id, before, after, user.user_id, "UI")
            propagate_to_contacts(uow.accounts, account, before, after, user.user_id)
            uow.commit()
            return self._record(uow, user, account_id)

    @staticmethod
    def _new_name(repo: AccountRepository, user: User, account: Account, raw: Any) -> str:
        name = clean_str(raw)
        if not name:
            raise BadRequestError("Company name is required")
        if name != account.name and repo.name_taken(user.tenant_id, name, except_id=account.account_id):
            raise ConflictError("A company with this name already exists.")
        return name

    def delete(self, user: User, account_id: str) -> dict:
        """Soft delete: hidden and restorable for 90 days; contacts are kept."""
        require_manager(user)
        if user.role == "AGENT":
            raise PermissionDenied("Agents cannot delete companies")
        with self.uow as uow:
            account = self._find(uow, user, account_id)
            if not can_edit_account(uow.session, user, account):
                raise PermissionDenied("You can only delete companies you or your team own")
            account.deleted_at, account.deleted_by = utcnow(), user.user_id
            uow.accounts.add(user.tenant_id, PropertyChange(
                tenant_id=user.tenant_id, object_type="COMPANY", object_id=account_id, field="deleted",
                new_value="deleted", source="UI", changed_by=user.user_id))
            uow.commit()
        return {"status": "deleted", "account_id": account_id, "restorable_days": crm.RESTORE_WINDOW_DAYS}

    def restore(self, user: User, account_id: str) -> dict:
        require_manager(user)
        with self.uow as uow:
            account = uow.accounts.deleted(user.tenant_id, account_id)
            if not account or not can_see_account(uow.session, user, account):
                raise NotFoundError("Deleted company not found")
            if account.domain and uow.accounts.domain_taken(user.tenant_id, account.domain, except_id=account_id,
                                                            live_only=True):
                raise ConflictError(f"Another company now uses {account.domain}")
            account.deleted_at, account.deleted_by = None, None
            uow.accounts.add(user.tenant_id, PropertyChange(
                tenant_id=user.tenant_id, object_type="COMPANY", object_id=account_id, field="deleted",
                old_value="deleted", source="UI", changed_by=user.user_id))
            uow.commit()
        return {"status": "restored", "account_id": account_id}

