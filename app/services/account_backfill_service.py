"""
Company backfill with preview, run history and undo (BR-CM-04, C-14).

Gives contacts that have no company one, the way new contacts get theirs: by the email's company domain
(creating the company when new), or by company name for personal email addresses.

  preview   dry run, writes nothing: what a run would create, link and refuse, with samples and conflicts
  run       batched (one commit per ~500 contacts, so completed work is kept if it stops), recorded as an
            ``AccountBackfillRun`` with one ``AccountBackfillItem`` per thing it did; re-running continues
  undo      removes the links the run made and the companies it created (only those nothing else uses or edited
            since), gives back the domains it set and restores the contact fields it filled -- each only where the
            record still holds what the run wrote

The preview and the run use the same planner (app/domain/accounts.py ``plan_backfill``) over the contacts the
user can see (BR-SH-02). Managers only (manage_prospects), as before.
"""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError, OperationFailed
from app.domain.accounts import BackfillContact, BackfillGroup, BackfillPlan, CompanyIndex, batches, plan_backfill
from app.models.account import Account
from app.models.account_backfill import AccountBackfillItem, AccountBackfillRun
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.accounts import AccountRepository, AccountsAccess, AccountsUnitOfWork, Session, accounts_uow
from app.services import contact_companies, crm
from app.services.contact_service import (
    can_see_owner,
    require_manager,
    sees_everything,
    visibility_scope,
    visible_user_ids,
)

logger = logging.getLogger(__name__)

__all__ = ["AccountBackfillService", "run_dict"]

STALE_RUN_MINUTES = 30
UNDO_CHUNK = 1000
SOURCE = "BACKFILL"


def run_dict(run: AccountBackfillRun, names: Optional[Dict[str, str]] = None) -> dict:
    return {
        "run_id": run.run_id, "status": run.status, "mode": run.mode, "stats": run.stats or {}, "error": run.error,
        "started_by": run.started_by, "started_by_name": (names or {}).get(run.started_by or ""),
        "started_at": run.started_at, "finished_at": run.finished_at, "undone_at": run.undone_at,
        "undone_by": run.undone_by, "can_undo": run.status in ("COMPLETED", "FAILED"),
    }


def _contact_rows(rows: Sequence[tuple[Any, ...]]) -> List[BackfillContact]:
    return [BackfillContact(prospect_id=r[0], email=r[1], company_name=r[2],
                            domain=crm.company_domain_from_email(r[1]), industry=r[3], emp_band=r[4])
            for r in rows]


def _first(values: Sequence[Optional[str]]) -> Optional[str]:
    return next((v for v in values if v), None)


class _Writer:
    """Applies one plan batch for a run and records each change as a run item."""

    def __init__(self, repo: AccountRepository, user: User, run: AccountBackfillRun, totals: Counter) -> None:
        self.repo, self.user, self.run, self.totals = repo, user, run, totals
        self.tenant_id = user.tenant_id

    def item(self, kind: str, *, account_id: Optional[str] = None, prospect_id: Optional[str] = None,
             field: Optional[str] = None, old: Optional[str] = None, new: Optional[str] = None) -> None:
        self.repo.add(self.tenant_id, AccountBackfillItem(
            run_id=self.run.run_id, tenant_id=self.tenant_id, kind=kind, account_id=account_id,
            prospect_id=prospect_id, field=field, old_value=old, new_value=new))

    def change(self, object_type: str, object_id: str, field: str, old: Optional[str], new: Optional[str]) -> None:
        self.repo.add(self.tenant_id, PropertyChange(
            tenant_id=self.tenant_id, object_type=object_type, object_id=object_id, field=field, old_value=old,
            new_value=new, source=SOURCE, changed_by=self.user.user_id))

    def apply(self, groups: Sequence[BackfillGroup]) -> None:
        ids = [c.prospect_id for g in groups for c in g.contacts]
        prospects = {p.prospect_id: p for p in self.repo.unlinked_by_ids(self.tenant_id, ids)}
        for group in groups:
            account = self.account_for(group)
            if account is None:
                self.totals["contacts_not_linked"] += len(group.contacts)
                continue
            for contact in group.contacts:
                prospect = prospects.get(contact.prospect_id)
                if prospect is None:  # linked, deleted or changed since the plan was made
                    self.totals["contacts_changed_since_plan"] += 1
                    continue
                self.link(prospect, account)

    def link(self, prospect: Prospect, account: Account) -> None:
        prospect.account_id = account.account_id
        self.item("CONTACT_LINKED", account_id=account.account_id, prospect_id=prospect.prospect_id)
        self.change("CONTACT", prospect.prospect_id, "account_id", None, account.account_id)
        self.totals["contacts_linked"] += 1
        if not (prospect.company_name or "").strip():
            old = prospect.company_name
            prospect.company_name = account.name
            self.item("CONTACT_FIELD", prospect_id=prospect.prospect_id, field="company_name", old=old,
                      new=account.name)
            self.change("CONTACT", prospect.prospect_id, "company_name", old, account.name)
            self.totals["company_names_filled"] += 1

    def account_for(self, group: BackfillGroup) -> Optional[Account]:
        account = self.repo.live(self.tenant_id, group.account_id) if group.account_id else None
        if account is None:
            return self.find_or_create(group)
        self.totals["existing_companies_used"] += 1
        if group.set_domain and not account.domain and not self.repo.domain_taken(self.tenant_id, group.set_domain):
            account.domain = group.set_domain
            self.item("ACCOUNT_DOMAIN_SET", account_id=account.account_id, old=None, new=group.set_domain)
            self.change("COMPANY", account.account_id, "domain", None, group.set_domain)
            self.totals["domains_set"] += 1
        return account

    def find_or_create(self, group: BackfillGroup) -> Optional[Account]:
        """Re-checked at write time: the company may exist by now, or its domain / name may be held."""
        existing = self.repo.live_by(self.tenant_id, domain=group.domain) if group.domain else \
            self.repo.live_by(self.tenant_id, name=group.name)
        if existing:
            self.totals["existing_companies_used"] += 1
            return existing
        if group.domain and self.repo.domain_taken(self.tenant_id, group.domain):
            return None  # held by a company in the recycle bin
        name = group.name
        if self.repo.name_taken(self.tenant_id, name):
            name = f"{name} ({group.domain})"[:255] if group.domain else name
            if self.repo.name_taken(self.tenant_id, name):
                return None
        return self.create(group, name)

    def create(self, group: BackfillGroup, name: str) -> Account:
        account = Account(tenant_id=self.tenant_id, name=name, domain=group.domain,
                          website=f"https://{group.domain}" if group.domain else None, lifecycle_stage="LEAD",
                          industry=_first([c.industry for c in group.contacts]),
                          emp_band=_first([c.emp_band for c in group.contacts]))
        self.repo.add(self.tenant_id, account)
        self.repo.flush()
        self.item("ACCOUNT_CREATED", account_id=account.account_id, new=name)
        self.change("COMPANY", account.account_id, "created", None, "Company created by backfill")
        self.totals["companies_created"] += 1
        return account


class _Undo:
    """Reverses one run's items, each only where the record still holds what the run wrote."""

    def __init__(self, ctx: AccountsAccess, user: User, run: AccountBackfillRun, commit: Callable[[], None]) -> None:
        self.ctx, self.repo, self.user, self.run, self.commit = ctx, ctx.accounts, user, run, commit
        self.counts: Counter = Counter()

    def items(self, kind: str) -> Any:
        offset = 0
        while True:
            chunk = self.repo.run_items(self.run.run_id, (kind,), offset, UNDO_CHUNK)
            if not chunk:
                return
            yield chunk
            offset += len(chunk)

    def change(self, object_type: str, object_id: str, field: str, old: Optional[str], new: Optional[str]) -> None:
        self.repo.add(self.user.tenant_id, PropertyChange(
            tenant_id=self.user.tenant_id, object_type=object_type, object_id=object_id, field=field,
            old_value=old, new_value=new, source=SOURCE, changed_by=self.user.user_id))

    def fields(self) -> None:
        for chunk in self.items("CONTACT_FIELD"):
            contacts = self.repo.contacts_by_ids(self.user.tenant_id, {i.prospect_id for i in chunk if i.prospect_id})
            for item in chunk:
                prospect = contacts.get(item.prospect_id or "")
                if prospect is None or not item.field or getattr(prospect, item.field, None) != item.new_value:
                    self.counts["fields_kept"] += 1
                    continue
                setattr(prospect, item.field, item.old_value)
                self.change("CONTACT", prospect.prospect_id, item.field, item.new_value, item.old_value)
                self.counts["fields_restored"] += 1
            self.commit()

    def links(self) -> None:
        for chunk in self.items("CONTACT_LINKED"):
            contacts = self.repo.contacts_by_ids(self.user.tenant_id, {i.prospect_id for i in chunk if i.prospect_id})
            for item in chunk:
                prospect = contacts.get(item.prospect_id or "")
                if prospect is None or prospect.account_id != item.account_id:
                    self.counts["links_kept"] += 1
                    continue
                prospect.account_id = None
                contact_companies.sync_primary(self.ctx.session, prospect, self.user.user_id)
                self.change("CONTACT", prospect.prospect_id, "account_id", item.account_id, None)
                self.counts["links_removed"] += 1
            self.commit()

    def domains(self) -> None:
        for chunk in self.items("ACCOUNT_DOMAIN_SET"):
            accounts = self.repo.accounts_by_ids(self.user.tenant_id, {i.account_id for i in chunk if i.account_id})
            for item in chunk:
                account = accounts.get(item.account_id or "")
                if account is None or account.domain != item.new_value:
                    self.counts["domains_kept"] += 1
                    continue
                account.domain = item.old_value
                self.change("COMPANY", account.account_id, "domain", item.new_value, item.old_value)
                self.counts["domains_restored"] += 1
            self.commit()

    def companies(self) -> None:
        for chunk in self.items("ACCOUNT_CREATED"):
            accounts = self.repo.accounts_by_ids(self.user.tenant_id, {i.account_id for i in chunk if i.account_id})
            for item in chunk:
                account = accounts.get(item.account_id or "")
                if account is None:
                    continue  # already purged
                if account.deleted_at is not None or self.repo.account_in_use(account.account_id) \
                        or self.repo.account_edited(account.account_id):
                    self.counts["companies_kept"] += 1
                    continue
                self.repo.purge_created(account)
                self.counts["companies_removed"] += 1
            self.commit()


class AccountBackfillService:
    """Backfill preview / run / history / undo for one request (each public method one unit of work)."""

    def __init__(self, uow: AccountsUnitOfWork, clock: Callable[[], datetime] = utcnow) -> None:
        self.uow = uow
        self.clock = clock

    @classmethod
    def for_session(cls, db: Session) -> "AccountBackfillService":
        return cls(accounts_uow(db))

    @staticmethod
    def _plan(ctx: AccountsAccess, user: User) -> BackfillPlan:
        rows = ctx.accounts.unlinked_contacts(user.tenant_id, visibility_scope(ctx.session, user))
        live, deleted = ctx.accounts.company_index_rows(user.tenant_id)
        return plan_backfill(_contact_rows(rows), CompanyIndex.build(live, deleted))

    def preview(self, user: User) -> dict:
        require_manager(user)
        with self.uow as uow:
            plan = self._plan(uow, user)
        return {"stats": plan.stats(), "samples": plan.samples(), "conflicts": plan.conflicts(),
                "generated_at": self.clock()}

    def run(self, user: User, mode: str = "RUN") -> dict:
        require_manager(user)
        with self.uow as uow:
            if uow.accounts.running_run(user.tenant_id, self.clock() - timedelta(minutes=STALE_RUN_MINUTES)):
                raise ConflictError("A backfill is already running for this workspace. Wait for it to finish.")
            run = AccountBackfillRun(tenant_id=user.tenant_id, started_by=user.user_id, mode=mode, status="RUNNING",
                                     started_at=self.clock())
            uow.accounts.add(user.tenant_id, run)
            uow.commit()
            run_id = run.run_id
            totals: Counter = Counter()
            try:
                plan = self._plan(uow, user)
                writer = _Writer(uow.accounts, user, run, totals)
                for batch in batches(plan.actionable()):
                    writer.apply(batch)
                    uow.commit()  # one commit per batch: completed work is never lost
                run.status, run.stats, run.finished_at = "COMPLETED", {**plan.stats(), **totals}, self.clock()
                uow.commit()
            except Exception as exc:
                uow.rollback()
                self._failed(uow, user, run_id, totals, exc)
                raise OperationFailed("The backfill stopped. Completed batches are kept; run it again to continue.",
                                      code="BACKFILL_FAILED") from exc
            return run_dict(run, self._names(uow, [run]))

    def _failed(self, ctx: AccountsUnitOfWork, user: User, run_id: str, totals: Counter, exc: Exception) -> None:
        logger.exception("company backfill %s failed (completed batches are kept)", run_id)
        run = ctx.accounts.run(user.tenant_id, run_id)
        if run is not None:
            run.status, run.error, run.stats, run.finished_at = "FAILED", str(exc)[:500], dict(totals), self.clock()
            ctx.commit()

    def legacy(self, user: User) -> dict:
        """POST /accounts/backfill (kept): a run without a preview, with the response it always had."""
        result = self.run(user, mode="DIRECT")
        return {"status": "ok", "linked_contacts": int(result["stats"].get("contacts_linked", 0)),
                "run_id": result["run_id"]}

    @staticmethod
    def _names(ctx: AccountsAccess, runs: Sequence[AccountBackfillRun]) -> Dict[str, str]:
        users = ctx.accounts.users_by_ids({r.started_by for r in runs if r.started_by})
        return {uid: f"{u.first_name or ''} {u.last_name or ''}".strip() for uid, u in users.items()}

    def list_runs(self, user: User, limit: int = 20) -> dict:
        require_manager(user)
        with self.uow as uow:
            started_by = None if sees_everything(user) else visible_user_ids(uow.session, user)
            runs = uow.accounts.runs(user.tenant_id, started_by, limit)
            names = self._names(uow, runs)
            return {"items": [run_dict(r, names) for r in runs]}

    def undo(self, user: User, run_id: str) -> dict:
        require_manager(user)
        with self.uow as uow:
            run = uow.accounts.run(user.tenant_id, run_id)
            if run is None or not (sees_everything(user) or can_see_owner(uow.session, user, run.started_by)):
                raise NotFoundError("Backfill run not found")
            if run.status == "UNDONE":
                raise ConflictError("This run was already undone.")
            stale = self.clock() - timedelta(minutes=STALE_RUN_MINUTES)
            if run.status == "RUNNING" and run.started_at and run.started_at > stale:
                raise ConflictError("This run is still in progress.")
            undo = _Undo(uow, user, run, uow.commit)
            undo.fields()
            undo.links()
            undo.domains()
            undo.companies()
            run.status, run.undone_at, run.undone_by = "UNDONE", self.clock(), user.user_id
            run.stats = {**(run.stats or {}), "undo": dict(undo.counts)}
            uow.commit()
            return run_dict(run, self._names(uow, [run]))
