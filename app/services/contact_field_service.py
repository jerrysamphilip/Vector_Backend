# app/services/contact_field_service.py
"""
Custom properties (BR-CM-09/10): definitions and value validation, for contacts and companies.

``clean_custom_value`` is pure (no database) so the per-type rules are unit-tested directly;
``clean_custom_fields`` applies it against the tenant's definitions. ``ContactFieldService`` serves the
custom properties endpoints in its own unit of work; SQL is in app/repositories/contacts.py.

Object types (C-16): a definition belongs to CONTACT (values in prospects.custom_fields) or COMPANY (values in
accounts.custom_fields). Every helper takes ``object_type`` (default CONTACT, the behaviour callers always had)
and only sees that type's definitions; field keys stay unique per tenant across both types.

Safety (C-17, rules in app/domain/custom_properties.py): the data type is fixed once created; choice options
are ``{value, label}`` (old plain-string options are read as such, never rewritten), so renaming a label keeps
stored values valid; DELETE deactivates (``is_active``): an inactive property is left out of the property list
(forms), required checks, writes and import mapping, but its values stay and ``label_map`` /
``definitions_in_order`` (exports, history) still include it; system properties cannot be deactivated.
"""

import re
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.domain.custom_properties import (
    PropertyRuleError,
    check_can_deactivate,
    check_type_unchanged,
    checked_max_length,
    normalize_options,
    option_value_for,
    option_values,
    parse_boolean,
    stored_options,
    text_limit,
)
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
from app.schemas.contact_schema import FieldOption, FieldWrite
from app.services.contact_service import clean_str, normalize_tags, require_manager

CHOICE_TYPES = ("SELECT", "RADIO", "MULTI_CHECKBOX")
CONTACT = "CONTACT"
COMPANY = "COMPANY"

_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FieldDefinitionLike(Protocol):
    label: str
    field_type: str
    options: Optional[List[Any]]


class _HasCustomFields(Protocol):
    custom_fields: Any


def object_type_of(d: Any) -> str:
    """A definition's object type; rows staged before a flush (or made before 0024) count as CONTACT."""
    return getattr(d, "object_type", None) or CONTACT


def of_type(definitions: Sequence[ContactFieldDefinition], object_type: str) -> List[ContactFieldDefinition]:
    return [d for d in definitions if object_type_of(d) == object_type]


def is_active(d: Any) -> bool:
    """Rows staged before a flush (and stand-ins) count as active."""
    return getattr(d, "is_active", None) is not False


def active(definitions: Sequence[ContactFieldDefinition]) -> List[ContactFieldDefinition]:
    return [d for d in definitions if is_active(d)]


def checked_object_type(object_type: str) -> str:
    if object_type not in OBJECT_TYPES:
        raise BadRequestError(f"object_type must be one of {list(OBJECT_TYPES)}")
    return object_type


def field_dict(d: ContactFieldDefinition) -> dict:
    """``options``: the stored values (what records hold, as before C-17); ``choices``: ``[{value, label}]``."""
    return {"field_id": d.field_id, "field_key": d.field_key, "label": d.label, "field_type": d.field_type,
            "options": option_values(d.options), "choices": stored_options(d.options),
            "sort_order": d.sort_order or 0, "group_name": d.group_name,
            "required": bool(d.required), "object_type": object_type_of(d),
            "is_active": is_active(d), "is_system": bool(getattr(d, "is_system", False)),
            "max_length": getattr(d, "max_length", None)}


def _raw_options(options: Optional[Sequence[Union[str, FieldOption]]]) -> Optional[list]:
    if options is None:
        return None
    return [o.model_dump() if isinstance(o, FieldOption) else o for o in options]


def _rule(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except PropertyRuleError as exc:
        raise BadRequestError(str(exc)) from None


def checked_field_type(field_type: str) -> str:
    field_type = field_type.strip().upper()
    if field_type not in FIELD_TYPES:
        raise BadRequestError(f"field_type must be one of {list(FIELD_TYPES)}")
    return field_type


def validate_field_type(field_type: str, options: Optional[Sequence[str]]) -> Tuple[str, Optional[list]]:
    """Normalised (field_type, options) for a definition; choice types need at least one option."""
    field_type = checked_field_type(field_type)
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
    """Validate and normalise one value for its definition. Blank values return None (= remove).
    A choice may be given by its value or its label; the value is stored."""
    label, ftype = definition.label, definition.field_type
    if is_blank(value):
        return None
    if ftype in ("TEXT", "LONG_TEXT"):
        return _clean_text(label, ftype, getattr(definition, "max_length", None), value)
    if ftype == "BOOLEAN":
        return _rule(parse_boolean, value, label)
    if ftype in CHOICE_TYPES:
        return _clean_choice(label, ftype, definition.options, value)
    if ftype == "NUMBER":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise BadRequestError(f"'{label}' must be a number") from None
        return int(value) if value.is_integer() else value
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


def _clean_text(label: str, ftype: str, max_length: Optional[int], value: Any) -> str:
    """Without a limit, text is cut at the type's length (as TEXT always was); over a set limit is an error."""
    text = str(value).strip()
    if max_length and len(text) > max_length:
        raise BadRequestError(f"'{label}' must be at most {max_length} characters")
    return text[:text_limit(ftype, max_length)]


def _clean_choice(label: str, ftype: str, options: Any, value: Any) -> Any:
    allowed = option_values(options)
    if ftype != "MULTI_CHECKBOX":
        chosen = option_value_for(options, value)
        if chosen is None:
            raise BadRequestError(f"'{label}' must be one of {allowed}")
        return chosen
    items = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
    matched = {item: option_value_for(options, item) for item in items}
    bad = [item for item, chosen in matched.items() if chosen is None]
    if bad:
        raise BadRequestError(f"'{label}' has unknown choices {bad}; allowed {allowed}")
    return [o for o in allowed if o in matched.values()]


def _repo(db: Union[Session, ContactRepository]) -> ContactRepository:
    return SqlAlchemyContactRepository(db) if isinstance(db, Session) else db


def clean_custom_fields(db: Union[Session, ContactRepository], tenant_id: str,
                        values: Optional[Dict[str, Any]], object_type: str = CONTACT) -> Dict[str, Any]:
    """Validate against the tenant's definitions of ``object_type``; blank values map to None (remove the key).
    Values for an inactive property are left out (the stored value is kept, C-17)."""
    defs = {d.field_key: d for d in of_type(_repo(db).field_definitions(tenant_id), object_type)}
    cleaned: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        definition = defs.get(key)
        if not definition:
            raise BadRequestError(f"Unknown custom property '{key}'")
        if is_active(definition):
            cleaned[key] = clean_custom_value(definition, value)
    return cleaned


def required_missing(db: Union[Session, ContactRepository], tenant_id: str,
                     record: Union[Prospect, _HasCustomFields], object_type: str = CONTACT) -> List[str]:
    """Labels of required custom properties the record (a contact, or a company) has no value for."""
    values = record.custom_fields or {}
    return [d.label for d in active(of_type(_repo(db).required_fields(tenant_id), object_type))
            if values.get(d.field_key) in (None, "", [])]


def label_map(db: Union[Session, ContactRepository], tenant_id: str, object_type: str = CONTACT) -> Dict[str, str]:
    """field_key -> label for the tenant's custom properties of ``object_type`` (inactive ones included)."""
    return {d.field_key: d.label for d in of_type(_repo(db).field_definitions(tenant_id), object_type)}


def definitions_in_order(db: Union[Session, ContactRepository], tenant_id: str,
                         object_type: str = CONTACT) -> List[ContactFieldDefinition]:
    """All properties of ``object_type`` in sort order, inactive ones included (exports keep their values)."""
    return of_type(_repo(db).field_definitions(tenant_id, order="sort"), object_type)


def create_field_definition(db: Union[Session, ContactRepository], tenant_id: str, label: str,
                            field_type: str = "TEXT", options: Optional[list] = None,
                            group_name: Optional[str] = None, required: bool = False,
                            object_type: str = CONTACT, max_length: Optional[int] = None) -> ContactFieldDefinition:
    """Shared with import (creating properties while mapping columns, BR-CM-26). Options are stored as
    ``{value, label}``. Caller commits."""
    repo = _repo(db)
    stored = (_rule(normalize_options, options) or None) if options is not None else None
    base_key = field_key_base(label)
    key, n = base_key, 2
    while repo.field_key_taken(tenant_id, key):
        key, n = f"{base_key}_{n}", n + 1
    definition = ContactFieldDefinition(tenant_id=tenant_id, field_key=key, label=label.strip(),
                                        field_type=field_type, options=stored, sort_order=repo.count_fields(tenant_id),
                                        group_name=clean_str(group_name), required=required,
                                        object_type=object_type, is_active=True, is_system=False,
                                        max_length=_rule(checked_max_length, field_type, max_length))
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

    def list(self, user: User, include_inactive: bool = False) -> List[dict]:
        """Active properties (what forms show); ``include_inactive`` adds the deactivated ones (settings)."""
        with self.uow as uow:
            definitions = self._definitions(uow, user.tenant_id, order="listing")
            return [field_dict(d) for d in (definitions if include_inactive else active(definitions))]

    def create(self, user: User, payload: FieldWrite) -> dict:
        require_manager(user)
        with self.uow as uow:
            field_type = checked_field_type(payload.field_type or "TEXT")
            options = _choice_options(field_type, payload.options, None)
            label = payload.label.strip()
            self._check_label_free(uow, user.tenant_id, label, None)
            definition = create_field_definition(uow.contacts, user.tenant_id, payload.label, field_type, options,
                                                 payload.group_name, payload.required, self.object_type,
                                                 payload.max_length)
            if payload.sort_order is not None:
                definition.sort_order = payload.sort_order
            uow.commit()
            return field_dict(definition)

    def update(self, user: User, field_id: str, payload: FieldWrite) -> dict:
        """Label, options, group, required, order, limit and active flag; the data type never changes."""
        require_manager(user)
        with self.uow as uow:
            definition = self._definition(uow, user.tenant_id, field_id)
            _rule(check_type_unchanged, definition.field_type, payload.field_type)
            if payload.is_active is False and is_active(definition):
                _rule(check_can_deactivate, bool(definition.is_system))
            label = payload.label.strip()
            self._check_label_free(uow, user.tenant_id, label, definition.field_id)
            if definition.field_type in CHOICE_TYPES and payload.options is not None:
                definition.options = _choice_options(definition.field_type, payload.options, definition.options)
            if "max_length" in payload.model_fields_set:
                definition.max_length = _rule(checked_max_length, definition.field_type, payload.max_length)
            definition.label = label
            definition.group_name = clean_str(payload.group_name)
            definition.required = payload.required
            if payload.sort_order is not None:
                definition.sort_order = payload.sort_order
            if payload.is_active is not None:
                definition.is_active = payload.is_active
            uow.commit()
            return field_dict(definition)

    def delete(self, user: User, field_id: str) -> dict:
        """Deactivates the property (C-17): hidden from forms and import mapping; stored values stay on the
        records and are still exported. PATCH ``is_active: true`` brings it back."""
        require_manager(user)
        with self.uow as uow:
            definition = self._definition(uow, user.tenant_id, field_id)
            _rule(check_can_deactivate, bool(definition.is_system))
            definition.is_active = False
            uow.commit()
        return {"status": "deactivated", "field_id": field_id}

    def _check_label_free(self, uow: ContactsUnitOfWork, tenant_id: str, label: str, own_id: Optional[str]) -> None:
        for d in self._definitions(uow, tenant_id):
            if d.field_id != own_id and d.label.lower() == label.lower():
                hint = "" if is_active(d) else " (deactivated: reactivate it instead)"
                raise ConflictError(f"A property called {label} already exists{hint}")


def _choice_options(field_type: str, options: Optional[Sequence[Union[str, FieldOption]]],
                    current: Any) -> Optional[list]:
    """``[{value, label}]`` for a choice type (at least one), None for the other types (options ignored)."""
    if field_type not in CHOICE_TYPES:
        return None
    cleaned = _rule(normalize_options, _raw_options(options), current)
    if not cleaned:
        raise BadRequestError("A choice property needs at least one option")
    return cleaned
