# app/services/contact_field_service.py
"""
Custom contact properties (BR-CM-09/10): definitions and value validation.

``clean_custom_value`` is pure (no database) so the per-type rules are unit-tested directly;
``clean_custom_fields`` applies it against the tenant's definitions.
"""

import re
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.contact_field import FIELD_TYPES, ContactFieldDefinition
from app.models.prospect import Prospect
from app.schemas.contact_schema import FieldWrite
from app.services.contact_service import clean_str, normalize_tags

CHOICE_TYPES = ("SELECT", "RADIO", "MULTI_CHECKBOX")

_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,25}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FieldDefinitionLike(Protocol):
    label: str
    field_type: str
    options: Optional[List[str]]


def field_dict(d: ContactFieldDefinition) -> dict:
    return {"field_id": d.field_id, "field_key": d.field_key, "label": d.label, "field_type": d.field_type,
            "options": d.options or [], "sort_order": d.sort_order or 0, "group_name": d.group_name,
            "required": bool(d.required)}


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


def clean_custom_fields(db: Session, tenant_id: str, values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate against the tenant's definitions; blank values map to None (remove the key)."""
    defs = {d.field_key: d for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id)}
    cleaned: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        definition = defs.get(key)
        if not definition:
            raise BadRequestError(f"Unknown custom property '{key}'")
        cleaned[key] = clean_custom_value(definition, value)
    return cleaned


def required_missing(db: Session, tenant_id: str, prospect: Prospect) -> List[str]:
    """Labels of required custom properties the contact has no value for."""
    required = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.required.is_(True)).all()
    values = prospect.custom_fields or {}
    return [d.label for d in required if values.get(d.field_key) in (None, "", [])]


def label_map(db: Session, tenant_id: str) -> Dict[str, str]:
    """field_key -> label for the tenant's custom properties."""
    return {d.field_key: d.label for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id)}


def definitions_in_order(db: Session, tenant_id: str) -> List[ContactFieldDefinition]:
    return db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id).order_by(ContactFieldDefinition.sort_order).all()


def create_field_definition(db: Session, tenant_id: str, label: str, field_type: str = "TEXT",
                            options: Optional[list] = None, group_name: Optional[str] = None,
                            required: bool = False) -> ContactFieldDefinition:
    """Shared with import (creating properties while mapping columns, BR-CM-26). Caller commits."""
    base_key = field_key_base(label)
    key, n = base_key, 2
    while db.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == tenant_id, ContactFieldDefinition.field_key == key).first():
        key, n = f"{base_key}_{n}", n + 1
    count = db.query(func.count(ContactFieldDefinition.field_id)).filter(
        ContactFieldDefinition.tenant_id == tenant_id).scalar()
    definition = ContactFieldDefinition(tenant_id=tenant_id, field_key=key, label=label.strip(),
                                        field_type=field_type, options=options, sort_order=count,
                                        group_name=clean_str(group_name), required=required)
    db.add(definition)
    db.flush()
    return definition


# ── Endpoint operations ────────────────────────────────────────────

def list_fields(db: Session, tenant_id: str) -> List[dict]:
    defs = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id
    ).order_by(ContactFieldDefinition.group_name, ContactFieldDefinition.sort_order,
               ContactFieldDefinition.created_at).all()
    return [field_dict(d) for d in defs]


def create_field(db: Session, tenant_id: str, payload: FieldWrite) -> dict:
    field_type, options = validate_field_type(payload.field_type, payload.options)
    if db.query(ContactFieldDefinition.field_id).filter(
            ContactFieldDefinition.tenant_id == tenant_id,
            func.lower(ContactFieldDefinition.label) == payload.label.strip().lower()).first():
        raise ConflictError(f"A property called {payload.label.strip()} already exists")
    definition = create_field_definition(db, tenant_id, payload.label, field_type, options,
                                         payload.group_name, payload.required)
    if payload.sort_order is not None:
        definition.sort_order = payload.sort_order
    db.commit()
    return field_dict(definition)


def update_field(db: Session, tenant_id: str, field_id: str, payload: FieldWrite) -> dict:
    definition = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.field_id == field_id,
        ContactFieldDefinition.tenant_id == tenant_id).first()
    if not definition:
        raise NotFoundError("Property not found")
    field_type, options = validate_field_type(payload.field_type, payload.options)
    definition.label = payload.label.strip()
    definition.field_type = field_type
    definition.options = options
    definition.group_name = clean_str(payload.group_name)
    definition.required = payload.required
    if payload.sort_order is not None:
        definition.sort_order = payload.sort_order
    db.commit()
    return field_dict(definition)


def delete_field(db: Session, tenant_id: str, field_id: str) -> None:
    """Removes the definition; stored values stay on contacts but are no longer shown."""
    deleted = db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.field_id == field_id,
        ContactFieldDefinition.tenant_id == tenant_id).delete()
    if not deleted:
        raise NotFoundError("Property not found")
    db.commit()
