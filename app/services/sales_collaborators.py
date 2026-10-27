"""
What the sales services need from other aggregates (ADR 0009, chunk A3), behind one Protocol.

Record history and workflow rules (crm), notifications, the sales-hierarchy visibility
(contact_service), note mentions, calendar push and merge-field rendering belong to aggregates that
have not moved to repositories yet; they still take a database session. ``ServiceCollaborators``
binds them to the unit of work's session; unit tests hand the sales services a fake instead.
"""
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set

from app.domain.sales import OwnerScope
from app.repositories.sales import SqlSession
from app.services import account_sync, crm, mentions, notifications
from app.services import contact_service as contacts
from app.services.email_sender_service import email_sender


class SalesCollaborators(Protocol):
    def owner_scope(self, user: Any) -> OwnerScope: ...
    def visible_user_ids(self, user: Any) -> Optional[Set[str]]: ...
    def team_user_ids(self, user: Any) -> Set[str]: ...
    def can_see_owner(self, user: Any, owner_id: Optional[str]) -> bool: ...
    def can_access_contact(self, user: Any, prospect: Any) -> bool: ...
    def sees_everything(self, user: Any) -> bool: ...
    def record_changes(self, tenant_id: str, object_type: str, object_id: str, before: dict, after: dict,
                       user_id: Optional[str], source: str = "UI") -> int: ...
    def notify(self, tenant_id: str, user_ids: Iterable[Optional[str]], kind: str, title: str,
               body: Optional[str] = None, link: Optional[str] = None, dedupe_key: Optional[str] = None,
               dedupe_days: int = 7) -> int: ...
    def manager_of(self, user_id: Optional[str]) -> Optional[str]: ...
    def resolve_company(self, tenant_id: str, email: Optional[str], company_name: Optional[str]) -> Any: ...
    def validate_mentions(self, user: Any, user_ids: Optional[List[str]], owner_id: Optional[str]) -> List[Any]: ...
    def save_mentions(self, user: Any, activity: Any, users: List[Any], record_name: str, link: str) -> None: ...
    def mentions_for(self, activity_ids: Iterable[str]) -> Dict[str, List[dict]]: ...
    def push_meeting(self, user: Any, activity: Any, deal_name: Optional[str]) -> None: ...
    def fill_merge_fields(self, text: str, contact: Any, sender: str) -> str: ...


class ServiceCollaborators:
    """``SalesCollaborators`` over the existing services, bound to one session."""

    def __init__(self, session: SqlSession) -> None:
        self._db = session

    def owner_scope(self, user: Any) -> OwnerScope:
        ids = contacts.visible_user_ids(self._db, user)
        if ids is None:
            return OwnerScope()
        return OwnerScope(frozenset(ids), contacts.can_manage_contacts(user))

    def visible_user_ids(self, user: Any) -> Optional[Set[str]]:
        return contacts.visible_user_ids(self._db, user)

    def team_user_ids(self, user: Any) -> Set[str]:
        return contacts.team_user_ids(self._db, user)

    def can_see_owner(self, user: Any, owner_id: Optional[str]) -> bool:
        return contacts.can_see_owner(self._db, user, owner_id)

    def can_access_contact(self, user: Any, prospect: Any) -> bool:
        return contacts.can_access_contact(user, prospect)

    def sees_everything(self, user: Any) -> bool:
        return contacts.sees_everything(user)

    def record_changes(self, tenant_id: str, object_type: str, object_id: str, before: dict, after: dict,
                       user_id: Optional[str], source: str = "UI") -> int:
        return crm.record_changes(self._db, tenant_id, object_type, object_id, before, after, user_id, source)

    def notify(self, tenant_id: str, user_ids: Iterable[Optional[str]], kind: str, title: str,
               body: Optional[str] = None, link: Optional[str] = None, dedupe_key: Optional[str] = None,
               dedupe_days: int = 7) -> int:
        return notifications.notify(self._db, tenant_id, user_ids, kind, title, body, link, dedupe_key, dedupe_days)

    def manager_of(self, user_id: Optional[str]) -> Optional[str]:
        return notifications.manager_of(self._db, user_id)

    def resolve_company(self, tenant_id: str, email: Optional[str], company_name: Optional[str]) -> Any:
        return crm.resolve_company(self._db, tenant_id, email, company_name)

    def validate_mentions(self, user: Any, user_ids: Optional[List[str]], owner_id: Optional[str]) -> List[Any]:
        return mentions.validate(self._db, user, user_ids, owner_id, True)

    def save_mentions(self, user: Any, activity: Any, users: List[Any], record_name: str, link: str) -> None:
        mentions.save(self._db, user, activity, users, record_name, link)

    def mentions_for(self, activity_ids: Iterable[str]) -> Dict[str, List[dict]]:
        return mentions.for_activities(self._db, activity_ids)

    def push_meeting(self, user: Any, activity: Any, deal_name: Optional[str]) -> None:
        account_sync.push_meeting(self._db, user, activity, deal_name)

    def fill_merge_fields(self, text: str, contact: Any, sender: str) -> str:
        return email_sender._substitute_placeholders(text, contact, sender)
