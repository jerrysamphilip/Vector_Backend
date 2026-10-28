# app/services/contact_field_service.py
"""
Custom properties (BR-CM-09/10): definitions and value validation, for contacts and companies.

``clean_custom_value`` is pure (no database) so the per-type rules are unit-tested directly;
``clean_custom_fields`` applies it against the tenant's definitions. ``ContactFieldService`` serves the
custom properties endpoints in its own unit of work; SQL is in app/repositories/contacts.py.

Object types (C-16): a definition belongs to CONTACT (values in prospects.custom_fields) or COMPANY (values in
accounts.custom_fields). Every helper takes ``object_type`` (default CONTACT, the behaviour callers always had)
and only sees that type's definitions; field keys stay unique per tenant across both types.
"""

import re
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.contact_field import FIELD_TYPES, OBJECT_TYPES, ContactFieldDefinition
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contacts import (
    ContactRepository,
    ContactsUnitOfWork,
    Session,
    SqlAlchemyContactRepository,
    contacts_uow,
)
from app.schemas.contact_schema import FieldWrite
from app.services.contact_service import clean_str, normalize_tags, require_manager

CHOICE_TYPES = ("SELECT", "RADIO", "MULTI_CHECKBOX")
CONTACT = "CONTACT"
COMPANY = "COMPANY"

_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FieldDefinitionLike(Protocol):
    label: str
    field_type: str
    options: Optional[List[str]]


class _HasCustomFields(Protocol):
    custom_fields: Any


def object_type_of(d: Any) -> str:
    """A definition's object type; rows staged before a flush (or made before 0024) count as CONTACT."""
    return getattr(d, "object_type", None) or CONTACT


def of_type(definitions: Sequence[ContactFieldDefinition], object_type: str) -> List[ContactFieldDefinition]:
    return [d for d in definitions if object_type_of(d) == object_type]


def checked_object_type(object_type: str) -> str:
    if object_type not in OBJECT_TYPES:
        raise BadRequestError(f"object_type must be one of {list(OBJECT_TYPES)}")
    return object_type


def field_dict(d: ContactFieldDefinition) -> dict:
    return {"field_id": d.field_id, "field_key": d.field_key, "label": d.label, "field_type": d.field_type,
            "options": d.options or [], "sort_order": d.sort_order or 0, "group_name": d.group_name,
            "required": bool(d.required), "object_type": object_type_of(d)}


def validate_field_type(field_type: str, options: Optional[Sequence[str]]) -> Tuple[str, Optional[list]]:
    """Normalised (field_type, options) for a definition; choice types need at least one option."""
    field_type = field_type.upper()
    if field_type not in FIELD_TYPES:
        raise BadRequestError(f"field_type must be one of {list(FIELD_TYPES)}")
    has_options = field_type in CHOICE_TYPES
    cleaned = normalize_tags(options) if has_options else None
    if has_options and not cleaned:
        raise BadRequestError("A choice property needs at least one option")
    return field_type, cleaned


def field_key_base(label: str) -> str:
    """Machine key derived from a label: lower-case, non-alphanumerics to '_', at most 50 chars."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:50] or "field"


def is_blank(value: Any) -> bool:
    return value is None or value == [] or (isinstance(value, str) and not value.strip())


def clean_custom_value(definition: FieldDefinitionLike, value: Any) -> Any:
    """Validate and normalise one value for its definition. Blank values return None (= remove)."""
    label, ftype, options = definition.label, definition.field_type, definition.options or []
    if is_blank(value):
        return None
    if ftype == "NUMBER":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise BadRequestError(f"'{label}' must be a number") from None
        return int(value) if value.is_integer() else value
    if ftype in ("SELECT", "RADIO"):
        if value not in options:
            raise BadRequestError(f"'{label}' must be one of {options}")
        return value
    if ftype == "MULTI_CHECKBOX":
        items = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
        bad = [v for v in items if v not in options]
        if bad:
            raise BadRequestError(f"'{label}' has unknown choices {bad}; allowed {options}")
        return [o for o in options if o in items]
    if ftype == "DATE":
        value = str(value).strip()[:10]
        if not _DATE_RE.match(value):
            raise BadRequestError(f"'{label}' must be a date (YYYY-MM-DD)")
        return value
    if ftype == "PHONE":
        value = str(value).strip()
        if not _PHONE_RE.match(value):
            raise BadRequestError(f"'{label}' must be a phone number")
        return value
    if ftype == "URL":
        value = str(value).strip()
        return value if re.match(r"^https?://", value) else f"https://{value}"
    return str(value).strip()[:1000]


def _repo(db: Union[Session, ContactRepository]) -> ContactRepository:
    return SqlAlchemyContactRepository(db) if isinstance(db, Session) else db


def clean_custom_fields(db: Union[Session, ContactRepository], tenant_id: str,
                        values: Optional[Dict[str, Any]], object_type: str = CONTACT) -> Dict[str, Any]:
    """Validate against the tenant's definitions of ``object_type``; blank values map to None (remove the key)."""
    defs = {d.field_key: d for d in of_type(_repo(db).field_definitions(tenant_id), object_type)}
    cleaned: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        definition = defs.get(key)
        if not definition:
            raise BadRequestError(f"Unknown custom property '{key}'")
        cleaned[key] = clean_custom_value(definition, value)
    return cleaned


def required_missing(db: Union[Session, ContactRepository], tenant_id: str,
                     record: Union[Prospect, _HasCustomFields], object_type: str = CONTACT) -> List[str]:
    """Labels of required custom properties the record (a contact, or a company) has no value for."""
    values = record.custom_fields or {}
    return [d.label for d in of_type(_repo(db).required_fields(tenant_id), object_type)
            if values.get(d.field_key) in (None, "", [])]


def label_map(db: Union[Session, ContactRepository], tenant_id: str, object_type: str = CONTACT) -> Dict[str, str]:
    """field_key -> label for the tenant's custom properties of ``object_type``."""
    return {d.field_key: d.label for d in of_type(_repo(db).field_definitions(tenant_id), object_type)}


def definitions_in_order(db: Union[Session, ContactRepository], tenant_id: str,
                         object_type: str = CONTACT) -> List[ContactFieldDefinition]:
    return of_type(_repo(db).field_definitions(tenant_id, order="sort"), object_type)


def create_field_definition(db: Union[Session, ContactRepository], tenant_id: str, label: str,
                            field_type: str = "TEXT", options: Optional[list] = None,
                            group_name: Optional[str] = None, required: bool = False,
                            object_type: str = CONTACT) -> ContactFieldDefinition:
    """Shared with import (creating properties while mapping columns, BR-CM-26). Caller commits."""
    repo = _repo(db)
    base_key = field_key_base(label)
    key, n = base_key, 2
    while repo.field_key_taken(tenant_id, key):
        key, n = f"{base_key}_{n}", n + 1
    definition = ContactFieldDefinition(tenant_id=tenant_id, field_key=key, label=label.strip(),
                                        field_type=field_type, options=options, sort_order=repo.count_fields(tenant_id),
                                        group_name=clean_str(group_name), required=required,
                                        object_type=object_type)
    repo.add(tenant_id, definition)
    repo.flush()
    return definition


# ── Endpoint operations ────────────────────────────────────────────

class ContactFieldService:
    """Custom property definitions of one object type (contacts by default, companies for /accounts/fields)
    for one request (each call is one unit of work)."""

    def __init__(self, uow: ContactsUnitOfWork, object_type: str = CONTACT) -> None:
        self.uow = uow
        self.object_type = checked_object_type(object_type)

    @classmethod
    def for_session(cls, db: Session, object_type: str = CONTACT) -> "ContactFieldService":
        return cls(contacts_uow(db), object_type)

    def _definitions(self, uow: ContactsUnitOfWork, tenant_id: str, order: str = "none") -> List[ContactFieldDefinition]:
        return of_type(uow.contacts.field_definitions(tenant_id, order=order), self.object_type)

    def _definition(self, uow: ContactsUnitOfWork, tenant_id: str, field_id: str) -> ContactFieldDefinition:
        definition = uow.contacts.field_definition(tenant_id, field_id)
        if not definition or object_type_of(definition) != self.object_type:
            raise NotFoundError("Property not found")
        return definition

    def list(self, user: User) -> List[dict]:
        with self.uow as uow:
            return [field_dict(d) for d in self._definitions(uow, user.tenant_id, order="listing")]

    def create(self, user: User, payload: FieldWrite) -> dict:
        require_manager(user)
        with self.uow as uow:
            field_type, options = validate_field_type(payload.field_type, payload.options)
            label = payload.label.strip()
            if any(d.label.lower() == label.lower() for d in self._definitions(uow, user.tenant_id)):
                raise ConflictError(f"A property called {label} already exists")
            definition = create_field_definition(uow.contacts, user.tenant_id, payload.label, field_type, options,
                                                 payload.group_name, payload.required, self.object_type)
            if payload.sort_order is not None:
                definition.sort_order = payload.sort_order
            uow.commit()
            return field_dict(definition)

    def update(self, user: User, field_id: str, payload: FieldWrite) -> dict:
        require_manager(user)
        with self.uow as uow:
            definition = self._definition(uow, user.tenant_id, field_id)
            field_type, options = validate_field_type(payload.field_type, payload.options)
            definition.label = payload.label.strip()
            definition.field_type = field_type
            definition.options = options
            definition.group_name = clean_str(payload.group_name)
            definition.required = payload.required
            if payload.sort_order is not None:
                definition.sort_order = payload.sort_order
            uow.commit()
            return field_dict(definition)

    def delete(self, user: User, field_id: str) -> dict:
        """Removes the definition; stored values stay on the records but are no longer shown."""
        require_manager(user)
        with self.uow as uow:
            self._definition(uow, user.tenant_id, field_id)
            if not uow.contacts.delete_field_definition(user.tenant_id, field_id):
                raise NotFoundError("Property not found")
            uow.commit()
        return {"status": "deleted", "field_id": field_id}
