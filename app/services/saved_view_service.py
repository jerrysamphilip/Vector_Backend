"""
Saved table views (BR-CM-19): filters, columns and sort, personal or shared. Shared views are
created and changed by admins; everyone sees their own and the shared ones. ``SavedViewService``
serves app/routers/crm_router.py, one unit of work per call; SQL is in app/repositories/contacts.py.
"""

from typing import Any, List

from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.models.crm import SavedView
from app.models.user import User
from app.repositories.contacts import ContactsUnitOfWork, Session, contacts_uow
from app.schemas.saved_view_schema import ViewWrite
from app.services import crm
from app.services.contact_service import can_manage_contacts, clean_str

VIEW_FIELDS = ("name", "shared", "filters", "columns", "sort")


def view_dict(v: SavedView, user: User) -> dict:
    return {"view_id": v.view_id, "name": v.name, "object_type": v.object_type, "shared": v.shared,
            "filters": v.filters, "columns": v.columns, "sort": v.sort, "owner_id": v.owner_id,
            "is_mine": v.owner_id == user.user_id,
            "can_edit": v.owner_id == user.user_id or (v.shared and user.role in ("SUPER_ADMIN", "ADMIN"))}


class SavedViewService:
    def __init__(self, uow: ContactsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "SavedViewService":
        return cls(contacts_uow(db))

    @staticmethod
    def _check_filters(uow: ContactsUnitOfWork, user: User, filters: Any) -> None:
        if filters:
            try:
                crm.parse_filter(uow.session, filters, user)
            except crm.FilterError as exc:
                raise BadRequestError(str(exc)) from exc

    @staticmethod
    def _get(uow: ContactsUnitOfWork, user: User, view_id: str, editing: bool = False) -> SavedView:
        view = uow.contacts.view(user.tenant_id, view_id)
        if not view or (not view.shared and view.owner_id != user.user_id):
            raise NotFoundError("View not found")
        if editing and not view_dict(view, user)["can_edit"]:
            raise PermissionDenied("Only the view's owner (or an admin, for shared views) can change it")
        return view

    def list(self, user: User, object_type: str) -> List[dict]:
        with self.uow as uow:
            return [view_dict(v, user) for v in uow.contacts.views(user.tenant_id, user.user_id, object_type)]

    def create(self, user: User, payload: ViewWrite) -> dict:
        name = clean_str(payload.name)
        if not name:
            raise BadRequestError("Give the view a name")
        if payload.shared and not can_manage_contacts(user):
            raise PermissionDenied("Only admins can share views")
        object_type = (payload.object_type or "CONTACT").upper()
        with self.uow as uow:
            if object_type == "CONTACT":
                self._check_filters(uow, user, payload.filters)
            view = SavedView(tenant_id=user.tenant_id, object_type=object_type, name=name,
                             owner_id=user.user_id, shared=bool(payload.shared), filters=payload.filters,
                             columns=payload.columns, sort=payload.sort)
            uow.contacts.add(user.tenant_id, view)
            uow.commit()
            return view_dict(view, user)

    def update(self, user: User, view_id: str, payload: ViewWrite) -> dict:
        with self.uow as uow:
            view = self._get(uow, user, view_id, editing=True)
            data = payload.model_dump(exclude_unset=True)
            if "shared" in data and data["shared"] and not can_manage_contacts(user):
                raise PermissionDenied("Only admins can share views")
            if "filters" in data and view.object_type == "CONTACT":
                self._check_filters(uow, user, data["filters"])
            if "name" in data and not clean_str(data["name"]):
                raise BadRequestError("Give the view a name")
            for field in VIEW_FIELDS:
                if field in data:
                    setattr(view, field, clean_str(data[field]) if field == "name" else data[field])
            uow.commit()
            return view_dict(view, user)

    def delete(self, user: User, view_id: str) -> dict:
        with self.uow as uow:
            uow.contacts.remove(self._get(uow, user, view_id, editing=True))
            uow.commit()
        return {"status": "deleted", "view_id": view_id}
