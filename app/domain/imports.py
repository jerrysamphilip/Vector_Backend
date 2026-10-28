"""
CRM import aggregate: options, limits, job states and the column-mapping vocabulary (ADR 0009, chunk A7).

Pure: no I/O, no SQLAlchemy, no FastAPI, no models. The router turns request fields into these
objects; app/services/import_service.py applies them; app/repositories/imports.py stores jobs and
saved mappings. Invalid values raise ``ImportInputError`` (a ``ValueError``); the service maps it to
a 400.

Modes (C-04):
    CREATE_AND_UPDATE  new e-mails create contacts, known ones are updated (the default)
    CREATE             only new contacts; rows matching an existing contact are skipped
    UPDATE_ONLY        only existing contacts; rows with an unknown e-mail are skipped
``update_existing`` (kept from the first version of the API) decides how an update treats values the
contact already has: overwrite them (true, the default) or only fill blanks (false).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Optional

# ── Mapping targets ────────────────────────────────────────────────

CONTACT_TARGETS = {
    "email": "Email", "first_name": "First name", "last_name": "Last name", "full_name": "Full name",
    "phone": "Phone", "mobile_phone": "Mobile", "designation": "Job title", "linkedin_url": "LinkedIn URL",
    "poc_city": "City", "poc_state": "State", "poc_country": "Country", "industry": "Industry",
    "emp_band": "Employees", "lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status",
    "lead_source": "Lead source", "legal_basis": "Legal basis", "tags": "Tags", "owner_email": "Owner email",
}
COMPANY_TARGETS = {
    "name": "Company name", "domain": "Company domain", "website": "Company website", "phone": "Company phone",
    "industry": "Company industry", "emp_band": "Company employees", "annual_revenue": "Annual revenue",
    "street": "Company street", "city": "Company city", "state": "Company state", "postal_code": "Company postal code",
    "country": "Company country",
}
# Header aliases used to suggest a mapping
ALIASES = {
    "contact.email": ["email", "email address", "e-mail", "work email", "business email"],
    "contact.first_name": ["first name", "firstname", "first", "given name"],
    "contact.last_name": ["last name", "lastname", "last", "surname", "family name"],
    "contact.full_name": ["name", "full name", "contact name", "contact"],
    "contact.phone": ["phone", "phone number", "work phone", "direct phone", "office phone", "poc phone", "telephone"],
    "contact.mobile_phone": ["mobile", "mobile phone", "mobile number", "cell", "cell phone"],
    "contact.designation": ["job title", "title", "designation", "position", "role"],
    "contact.linkedin_url": ["linkedin", "linkedin url", "poc linkedin", "linkedin profile"],
    "contact.poc_city": ["city", "poc city"], "contact.poc_state": ["state", "poc state", "region", "province"],
    "contact.poc_country": ["country", "poc country"], "contact.industry": ["industry"],
    "contact.emp_band": ["emp band", "employees", "company size", "employee count"],
    "contact.lifecycle_stage": ["lifecycle stage", "lifecycle", "stage"],
    "contact.lead_status": ["lead status", "status"], "contact.lead_source": ["lead source", "source"],
    "contact.tags": ["tags", "labels"], "contact.owner_email": ["owner", "owner email", "contact owner"],
    "company.name": ["company", "company name", "organization", "organisation", "account", "account name"],
    "company.domain": ["domain", "company domain", "email domain"],
    "company.website": ["website", "company website", "url", "web"],
    "company.annual_revenue": ["annual revenue", "revenue"],
    "company.phone": ["company phone"], "company.street": ["address", "street", "street address"],
    "company.postal_code": ["postal code", "zip", "zip code", "postcode"],
}
# Contact property -> company field of the same meaning, used when a file has no Email column
CONTACT_TO_COMPANY = {
    "contact.full_name": "company.name", "contact.phone": "company.phone", "contact.industry": "company.industry",
    "contact.emp_band": "company.emp_band", "contact.poc_city": "company.city", "contact.poc_state": "company.state",
    "contact.poc_country": "company.country",
}
COMPANY_KEYS = frozenset({"company.name", "company.domain", "company.website"})


class ImportInputError(ValueError):
    """A request value the import cannot use; the message is shown to the user."""


class ImportMode(str, Enum):
    CREATE_AND_UPDATE = "CREATE_AND_UPDATE"
    CREATE = "CREATE"
    UPDATE_ONLY = "UPDATE_ONLY"

    @property
    def creates(self) -> bool:
        return self is not ImportMode.UPDATE_ONLY

    @property
    def updates(self) -> bool:
        return self is not ImportMode.CREATE


class DateOrder(str, Enum):
    """How an ambiguous date such as 03/04/2026 is read."""

    MDY = "MDY"
    DMY = "DMY"


class JobStatus(str, Enum):
    UPLOADED = "UPLOADED"    # file stored, waiting for a mapping
    QUEUED = "QUEUED"        # started; the worker picks it up
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
TERMINAL_STATUSES = (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)


@dataclass(frozen=True)
class ImportLimits:
    """File limits (C-03). ``sync_max_rows``: larger files run as a background job."""

    max_rows: int = 50_000
    max_columns: int = 200
    max_bytes: int = 15 * 1024 * 1024
    sync_max_rows: int = 1_000

    def __post_init__(self) -> None:
        if min(self.max_rows, self.max_columns, self.max_bytes, self.sync_max_rows) < 1:
            raise ImportInputError("Import limits must be positive")


def _text(value: Any, limit: int) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _enum(kind: type[Enum], value: Any, default: Enum, what: str) -> Any:
    if value in (None, ""):
        return default
    try:
        return kind(str(value).strip().upper())
    except ValueError:
        allowed = ", ".join(str(m.value) for m in kind)
        raise ImportInputError(f"{what} must be one of {allowed}") from None


@dataclass(frozen=True)
class ImportOptions:
    """What the person chose for one import (C-04), validated."""

    mode: ImportMode = ImportMode.CREATE_AND_UPDATE
    update_existing: bool = True
    owner_id: Optional[str] = None
    list_id: Optional[str] = None
    new_list_name: Optional[str] = None
    default_source: Optional[str] = None
    date_order: DateOrder = DateOrder.MDY
    sheet: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: Optional[Mapping[str, Any]]) -> ImportOptions:
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ImportInputError("options must be a JSON object")
        return cls(
            mode=_enum(ImportMode, raw.get("mode"), ImportMode.CREATE_AND_UPDATE, "mode"),
            update_existing=raw.get("update_existing", True) is not False,
            owner_id=_text(raw.get("owner_id"), 36),
            list_id=_text(raw.get("list_id"), 36),
            new_list_name=_text(raw.get("new_list_name"), 255),
            default_source=_text(raw.get("default_source"), 100),
            date_order=_enum(DateOrder, raw.get("date_order"), DateOrder.MDY, "date_order"),
            sheet=_text(raw.get("sheet"), 255),
        )

    def with_list(self, list_id: str) -> ImportOptions:
        return replace(self, list_id=list_id, new_list_name=None)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "update_existing": self.update_existing, "owner_id": self.owner_id,
                "list_id": self.list_id, "new_list_name": self.new_list_name,
                "default_source": self.default_source, "date_order": self.date_order.value, "sheet": self.sheet}


def parse_mapping(raw: Any) -> dict[str, str]:
    """{column: target} from the request; targets are checked against the workspace later."""
    if not isinstance(raw, Mapping):
        raise ImportInputError("mapping must be a JSON object {column: target}")
    out: dict[str, str] = {}
    for column, target in raw.items():
        if not isinstance(column, str) or not (target is None or isinstance(target, str)):
            raise ImportInputError("mapping must map column names to target names")
        if target and target != "skip":
            out[column] = target
    return out


@dataclass(frozen=True)
class NewProperty:
    """A "new:<TYPE>:<Label>" mapping target: a custom property created when the import starts."""

    field_type: str
    label: str

    @classmethod
    def parse(cls, target: str, field_types: tuple[str, ...]) -> Optional[NewProperty]:
        if not target.startswith("new:"):
            return None
        _, ftype, label = (target.split(":", 2) + ["", ""])[:3]
        ftype = (ftype or "TEXT").upper()
        if ftype not in field_types or not label.strip():
            raise ImportInputError("bad new property")
        return cls(ftype, label.strip())


SAVED_MAPPING_NAME = re.compile(r"^[\w .,&()'/+-]{1,100}$")


@dataclass(frozen=True)
class SavedMappingInput:
    """A named column mapping a workspace reuses (C-04), with the options that go with it."""

    name: str
    mapping: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, name: Any, mapping: Any, options: Any = None) -> SavedMappingInput:
        clean = " ".join(str(name or "").split())
        if not SAVED_MAPPING_NAME.match(clean):
            raise ImportInputError("Name the mapping (up to 100 letters, digits and punctuation)")
        parsed = parse_mapping(mapping)
        if not parsed:
            raise ImportInputError("The mapping has no mapped columns")
        if len(parsed) > 500:
            raise ImportInputError("The mapping has too many columns")
        opts = ImportOptions.from_raw(options).as_dict() if options else {}
        for transient in ("list_id", "new_list_name", "sheet"):
            opts.pop(transient, None)
        return cls(clean, parsed, opts)


@dataclass
class Tally:
    """Row outcome counts of a run or a dry run."""

    contacts_created: int = 0
    contacts_updated: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"contacts_created": self.contacts_created, "contacts_updated": self.contacts_updated,
                "companies_created": self.companies_created, "companies_updated": self.companies_updated,
                "skipped": self.skipped}

    def copy(self) -> Tally:
        return replace(self)


def progress_percent(processed: Optional[int], total: Optional[int]) -> int:
    if not total:
        return 0
    return max(0, min(100, int((processed or 0) * 100 / total)))


def suggest_mapping(headers: list[str], custom: Mapping[str, str]) -> dict[str, str]:
    """Header -> target suggestion from the aliases and the workspace's custom property labels."""
    taken: set[str] = set()
    mapping: dict[str, str] = {}
    for h in headers:
        key = re.sub(r"\s+", " ", h.strip().lower().replace("_", " "))
        target = next((t for t, names in ALIASES.items() if key in names and t not in taken), None)
        if not target:
            target = next((f"custom.{k}" for k, label in custom.items()
                           if key in (label.lower(), k.replace("_", " ")) and f"custom.{k}" not in taken), None)
        mapping[h] = target or "skip"
        if target:
            taken.add(target)
    if "contact.email" not in taken:
        _company_only(mapping, taken)
    return mapping


def _company_only(mapping: dict[str, str], taken: set[str]) -> None:
    """No Email column: a company-only file. Contact properties can't be imported without an email to
    match on, so suggest the company field of the same meaning, else skip."""
    for h, target in mapping.items():
        if not target.startswith(("contact.", "custom.")):
            continue
        company_target = CONTACT_TO_COMPANY.get(target)
        if company_target and company_target not in taken:
            mapping[h] = company_target
            taken.add(company_target)
        else:
            mapping[h] = "skip"
