"""
Contact lists (BR-CM-22/23/24; gap list C-21): STATIC lists hold a fixed set of contacts, ACTIVE lists
are a saved filter evaluated whenever the list is read. Serves app/routers/lists_router.py, one unit of
work per method (ADR 0009, chunk A1).

Anyone in the workspace can read lists (member counts follow their own visibility, BR-SH-02); creating,
editing, deleting and changing members needs manage_prospects. List names are unique per workspace
(case-insensitive): a clash is a 409 with a readable message. Deleting a list never deletes contacts.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.core.clock import utcnow
from app.domain.contacts import ContactFilter, FilterError
from app.domain.lists import (
    MAX_UNPAGED_LISTS,
    NAME_TAKEN,
    ONLY_ACTIVE_FILTERS,
    ListFilter,
    ListRuleError,
    list_type_of,
    new_members,
    require_conditions,
    require_name,
    require_static,
)
from app.domain.paging import Paging
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User
from app.repositories.lists import ListRepository, ListsUnitOfWork, Session, lists_uow
from app.services import crm
from app.services.contact_query_service import ExportFile, export_contacts
from app.services.contact_service import NO_MANAGE_PERMISSION, can_manage_contacts, clean_str, visibility_scope
from app.repositories.contacts import borrow

__all__ = ["ListService"]


def _require_manager(user: User) -> None:
    if not can_manage_contacts(user):
        raise PermissionDenied(NO_MANAGE_PERMISSION)


def _rule(fn: Any, *args: Any) -> Any:
    """Run a domain rule; its ListRuleError becomes a 400."""
    try:
        return fn(*args)
    except ListRuleError as exc:
        raise BadRequestError(str(exc)) from exc


class ListService:
    def __init__(self, uow: ListsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> ListService:
        return cls(lists_uow(db))

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _get(uow: ListsUnitOfWork, user: User, list_id: str) -> ProspectList:
        plist = uow.lists.get(user.tenant_id, list_id)
        if not plist:
            raise NotFoundError("List not found")
        return plist

    @staticmethod
    def _count(uow: ListsUnitOfWork, user: User, plist: ProspectList) -> int:
        try:
            scope = crm.list_scope(uow.session, plist, user)
        except FilterError:
            return 0
        return uow.lists.visible_member_count(scope, visibility_scope(uow.session, user))

    def _dict(self, uow: ListsUnitOfWork, user: User, plist: ProspectList,
              owners: Optional[dict[str, str]] = None) -> dict:
        owners = owners or {}
        return {
            "list_id": plist.list_id, "list_name": plist.list_name, "list_type": plist.list_type or "STATIC",
            "source_type": plist.source_type, "filters": plist.filters, "description": plist.description,
            "created_by": plist.uploaded_by, "created_by_name": owners.get(plist.uploaded_by),
            "created_at": plist.uploaded_at, "member_count": self._count(uow, user, plist),
        }

    @staticmethod
    def _check_filters(uow: ListsUnitOfWork, user: User, filters: Any) -> None:
        _rule(require_conditions, filters)
        try:
            crm.filter_clause(uow.session, filters, user)
        except FilterError as exc:
            raise BadRequestError(str(exc)) from exc

    @staticmethod
    def _check_name(repo: ListRepository, user: User, name: str, except_id: Optional[str] = None) -> None:
        if repo.name_taken(user.tenant_id, name, except_id):
            raise ConflictError(NAME_TAKEN, detail={"message": NAME_TAKEN, "field": "list_name"})

    # ── reads ────────────────────────────────────────────────────────
    def list(self, user: User, f: ListFilter, paging: Optional[Paging] = None) -> dict:
        """Lists, newest first. Without ``paging`` the original response (up to 500, ``items`` only);
        with it ``items`` / ``total`` / ``page`` / ``page_size`` (C-21)."""
        with self.uow as uow:
            repo = uow.lists
            if paging is None:
                rows, extra = repo.search(user.tenant_id, f, MAX_UNPAGED_LISTS), {}
            else:
                page = repo.page(user.tenant_id, f, paging)
                rows = list(page.items)
                extra = {"total": page.total, "page": paging.page, "page_size": paging.page_size}
            owners = repo.user_names({r.uploaded_by for r in rows})
            return {"items": [self._dict(uow, user, r, owners) for r in rows], **extra}

    def get(self, user: User, list_id: str) -> dict:
        with self.uow as uow:
            return self._dict(uow, user, self._get(uow, user, list_id))

    def preview(self, user: User, filters: Any) -> dict:
        """How many contacts a filter matches right now (for building active lists)."""
        with self.uow as uow:
            try:
                tree = crm.parse_filter(uow.session, filters, user)
            except FilterError as exc:
                raise BadRequestError(str(exc)) from exc
            count, sample = uow.lists.preview(user.tenant_id, visibility_scope(uow.session, user), tree, 5)
            return {"count": count, "sample": [{"prospect_id": p.prospect_id, "full_name": p.full_name,
                                                "email": p.email} for p in sample]}

    # ── writes ───────────────────────────────────────────────────────
    def create(self, user: User, list_name: Any, list_type: Optional[str], filters: Any,
               description: Optional[str]) -> dict:
        _require_manager(user)
        name = _rule(require_name, list_name)
        kind = _rule(list_type_of, list_type)
        with self.uow as uow:
            if kind == "ACTIVE":
                self._check_filters(uow, user, filters)
            self._check_name(uow.lists, user, name)
            plist = ProspectList(tenant_id=user.tenant_id, list_name=name, list_type=kind, source_type="MANUAL",
                                 filters=filters if kind == "ACTIVE" else None,
                                 description=clean_str(description), uploaded_by=user.user_id)
            uow.lists.add(user.tenant_id, plist)
            uow.commit()
            return self._dict(uow, user, plist)

    def update(self, user: User, list_id: str, data: dict) -> dict:
        _require_manager(user)
        with self.uow as uow:
            plist = self._get(uow, user, list_id)
            if "list_name" in data:
                name = _rule(require_name, data["list_name"])
                self._check_name(uow.lists, user, name, plist.list_id)
                plist.list_name = name
            if "description" in data:
                plist.description = clean_str(data["description"])
            if "filters" in data:
                if plist.list_type != "ACTIVE":
                    raise BadRequestError(ONLY_ACTIVE_FILTERS)
                self._check_filters(uow, user, data["filters"])
                plist.filters = data["filters"]
            uow.commit()
            return self._dict(uow, user, plist)

    def delete(self, user: User, list_id: str) -> dict:
        """Delete the list only; its contacts are kept."""
        _require_manager(user)
        with self.uow as uow:
            plist = self._get(uow, user, list_id)
            uow.lists.delete_members(list_id)
            uow.lists.remove(plist)
            uow.commit()
        return {"status": "deleted", "list_id": list_id}

    def add_members(self, user: User, list_id: str, prospect_ids: Sequence[str]) -> dict:
        _require_manager(user)
        with self.uow as uow:
            repo = uow.lists
            plist = self._get(uow, user, list_id)
            _rule(require_static, plist.list_type)
            valid = repo.live_prospect_ids(user.tenant_id, prospect_ids)
            existing = repo.member_ids(list_id, valid)
            added = new_members(prospect_ids, valid, existing)
            now = utcnow()
            for pid in added:
                repo.stage(ProspectListMember(list_id=list_id, prospect_id=pid, is_new_prospect=False, added_at=now))
            uow.commit()
        return {"added": len(added), "already_in_list": len(existing),
                "not_found": len(set(prospect_ids) - valid)}

    def remove_members(self, user: User, list_id: str, prospect_ids: Sequence[str]) -> dict:
        _require_manager(user)
        with self.uow as uow:
            plist = self._get(uow, user, list_id)
            _rule(require_static, plist.list_type)
            removed = uow.lists.remove_members(list_id, prospect_ids)
            uow.commit()
        return {"removed": removed}

    def export(self, user: User, list_id: str, fmt: str, columns: Optional[str]) -> ExportFile:
        """The list's members (those the user may see) as CSV / XLSX; logged as a LIST export (C-07)."""
        with self.uow as uow:
            plist = self._get(uow, user, list_id)
            export = export_contacts(borrow(uow.session), user, fmt, columns, ContactFilter(list_id=plist.list_id),
                                     object_type="LIST", list_name=plist.list_name)
            uow.commit()
            return export
