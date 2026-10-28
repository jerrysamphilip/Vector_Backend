# app/services/import_rows.py
"""
The CRM import row engine (BR-CM-25..30, C-03..C-05): turns mapped rows into contacts and companies.

``ImportPlan`` is a validated column mapping for one workspace; ``RowEngine`` applies it to chunks of
rows inside the caller's transaction (the import service commits per chunk). Contacts are matched by
e-mail (unique per workspace) and companies by domain, then name. New rows are inserted under the
database's unique keys, so several imports at once can't create duplicates: a collision becomes an
update. Each row runs in its own savepoint: a refused value undoes that row only and becomes its error.

``RowEngine.evaluate_chunk`` is the dry run: the same parsing and matching, read-only, counting what
would be created, updated, skipped or refused.

No SQL here (app/repositories/imports.py); legacy helpers that take a ``db`` (property history,
custom property definitions, visibility) get the unit of work's session.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.core.clock import utcnow
from app.core.errors import DomainError
from app.domain.imports import COMPANY_KEYS, COMPANY_TARGETS, CONTACT_TARGETS, ImportOptions, NewProperty, Tally
from app.models.account import Account
from app.models.contact_field import FIELD_TYPES
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectListMember
from app.models.user import User
from app.repositories.imports import DuplicateRow, ImportsUnitOfWork, RejectedValue
from app.services import crm, import_limits
from app.services.contact_field_service import clean_custom_value, create_field_definition
from app.services.contact_service import normalize_tags, visibility_scope
from app.services.import_parsing import (
    ValueProblem,
    clean_phone,
    custom_value,
    data_error_reason,
    email_address,
    parse_number,
    split_name,
)
from app.utils.business_calendar import get_timezone_for_state
from app.utils.company_domain import is_valid_domain

NOT_VISIBLE = "Contact already exists, owned by someone outside your visibility"
MULTI_VALUE_TYPES = ("SELECT", "RADIO", "MULTI_CHECKBOX")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTACT_LIMITS = import_limits.column_limits(Prospect)
COMPANY_LIMITS = import_limits.column_limits(Account)
ALL_LABELS = {**COMPANY_TARGETS, **CONTACT_TARGETS}

Row = Dict[str, Any]
Chunk = Sequence[Tuple[int, Row]]


class RowError(Exception):
    """This row cannot be imported; the message is its reason in the error report."""


class MappingError(ValueError):
    """The mapping cannot be used; the message is shown to the user (400)."""


def check_lengths(values: Dict[str, Any], limits: Dict[str, int], labels: Dict[str, str]) -> None:
    """A value longer than its column is this row's error (with the field), not a failed import."""
    problems = import_limits.oversized_fields(values, limits, labels)
    if problems:
        raise RowError("; ".join(problems))


def pick(value: Any, choices: Dict[str, str], name: str) -> Optional[str]:
    """A code from its code or label, ignoring case; RowError otherwise."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    for code, label in choices.items():
        if v.upper().replace(" ", "_") == code or v.lower() == label.lower():
            return code
    raise RowError(f"{name} '{v}' is not one of: {', '.join(choices.values())}")


def company_domain(cvals: Dict[str, Any], contact_email: Optional[str]) -> Optional[str]:
    """The company domain for a row: the Domain column (must be valid), else a usable Website,
    else the contact's work email domain. Free-mail domains never identify a company."""
    raw = cvals.get("domain")
    domain = crm.clean_domain(raw)
    if domain and not is_valid_domain(domain):
        raise RowError(f"Company domain '{raw}' is not a valid domain")
    if not domain:
        website = crm.clean_domain(cvals.get("website"))
        domain = website if website and is_valid_domain(website) else None
    if domain and crm.is_personal_domain(domain):
        domain = None
    return domain or crm.company_domain_from_email(contact_email)


# ── Plan ───────────────────────────────────────────────────────────

@dataclass
class PendingProperty:
    """A custom property the mapping creates when the import starts (the dry run only validates)."""

    field_key: str
    label: str
    field_type: str
    options: Optional[list] = None
    required: bool = False


@dataclass
class ImportPlan:
    resolved: Dict[str, str]                 # column -> contact.x / company.x / custom.key
    custom_defs: Dict[str, Any]
    has_contacts: bool
    has_companies: bool

    @property
    def required_defs(self) -> List[Any]:
        return [d for d in self.custom_defs.values() if getattr(d, "required", False)]


def _new_property_options(rows: Sequence[Row], column: str) -> List[str]:
    values: set = set()
    for row in rows:
        value = row.get(column)
        if value is not None:
            values.update(x.strip() for x in str(value).split(";") if x.strip())
    return sorted(values)[:200] or ["Yes"]


def build_plan(uow: ImportsUnitOfWork, tenant_id: str, mapping: Dict[str, str], headers: Sequence[str],
               rows: Sequence[Row], create_properties: bool) -> ImportPlan:
    """Check the mapping against the file and the workspace. "new:" targets create custom properties
    (BR-CM-26) when ``create_properties``, else stand in as pending ones (dry run)."""
    custom_defs: Dict[str, Any] = {d.field_key: d for d in uow.imports.field_definitions(tenant_id)}
    resolved: Dict[str, str] = {}
    for column, target in mapping.items():
        if column not in headers or not target or target == "skip":
            continue
        try:
            new = NewProperty.parse(target, FIELD_TYPES)
        except ValueError:
            raise MappingError(f"Bad new property for column '{column}'") from None
        if new is not None:
            target = _new_property_target(uow, tenant_id, new, column, rows, custom_defs, create_properties)
        kind, _, name = target.partition(".")
        known = (kind == "contact" and name in CONTACT_TARGETS) or (kind == "company" and name in COMPANY_TARGETS) \
            or (kind == "custom" and name in custom_defs)
        if not known:
            raise MappingError(f"Unknown target '{target}' for column '{column}'")
        resolved[column] = target
    return _checked_plan(resolved, custom_defs)


def _new_property_target(uow: ImportsUnitOfWork, tenant_id: str, new: NewProperty, column: str,
                         rows: Sequence[Row], custom_defs: Dict[str, Any], create: bool) -> str:
    options = _new_property_options(rows, column) if new.field_type in MULTI_VALUE_TYPES else None
    if create:
        definition: Any = create_field_definition(uow.session, tenant_id, new.label, new.field_type, options)
    else:
        definition = PendingProperty(f"new:{column}", new.label, new.field_type, options)
    custom_defs[definition.field_key] = definition
    return f"custom.{definition.field_key}"


def _checked_plan(resolved: Dict[str, str], custom_defs: Dict[str, Any]) -> ImportPlan:
    targets = set(resolved.values())
    has_contacts = "contact.email" in targets
    has_companies = bool(COMPANY_KEYS & targets)
    if not has_contacts and not has_companies:
        raise MappingError("Map an Email column (contacts) or a Company name/domain column (companies)")
    if not has_contacts and any(t.startswith(("contact.", "custom.")) for t in targets):
        raise MappingError("Contact properties need an Email column to match contacts")
    return ImportPlan(resolved, custom_defs, has_contacts, has_companies)


# ── Engine ─────────────────────────────────────────────────────────

@dataclass
class EngineState:
    tally: Tally = field(default_factory=Tally)
    errors: List[dict] = field(default_factory=list)
    seen_emails: Dict[str, int] = field(default_factory=dict)
    companies_touched: set = field(default_factory=set)
    list_members: List[str] = field(default_factory=list)

    def mark(self) -> Tuple[Tally, int, set, int, Dict[str, int]]:
        return (self.tally.copy(), len(self.errors), set(self.companies_touched), len(self.list_members),
                dict(self.seen_emails))

    def restore(self, saved: Tuple[Tally, int, set, int, Dict[str, int]]) -> None:
        """Undo the bookkeeping of a chunk that was rolled back (deadlock retry)."""
        self.tally = saved[0].copy()
        del self.errors[saved[1]:]
        self.companies_touched = set(saved[2])
        del self.list_members[saved[3]:]
        self.seen_emails = dict(saved[4])


def error_entry(row_no: int, reason: str, row: Row) -> dict:
    return {"row": row_no, "reason": reason, "values": {k: (None if v is None else str(v)) for k, v in row.items()}}


class RowEngine:
    """Applies one import's plan to chunks of rows for ``user`` (BR-SH-02 visibility applies)."""

    def __init__(self, uow: ImportsUnitOfWork, user: User, plan: ImportPlan, options: ImportOptions,
                 file_name: str, state: Optional[EngineState] = None) -> None:
        self.uow = uow
        self.user = user
        self.tenant_id = user.tenant_id
        self.plan = plan
        self.options = options
        self.file_name = file_name
        self.state = state or EngineState()
        users = uow.imports.users(self.tenant_id)
        self.owners_by_email = {u.email.lower(): u.user_id for u in users if u.email}
        self.owners_by_name = _owners_by_name(users)
        self.unsubscribed = uow.imports.unsubscribed_emails(self.tenant_id) if plan.has_contacts else set()
        self.scope = visibility_scope(uow.session, user)

    @property
    def repo(self) -> Any:
        return self.uow.imports

    # ── values ──
    def values_for(self, row: Row, kind: str) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {}
        for column, target in self.plan.resolved.items():
            k, _, name = target.partition(".")
            if k == kind:
                value = row.get(column)
                out[name] = str(value).strip() if value is not None and str(value).strip() != "" else None
        return out

    def owner_id_for(self, text: str) -> str:
        """The owner column holds a user's e-mail (also "Name <email>") or full name."""
        email = (email_address(text) or "").lower()
        uid = self.owners_by_email.get(email)
        if uid is None:
            matches = self.owners_by_name.get(" ".join(text.lower().split()), [])
            if len(matches) > 1:
                raise RowError(f"Owner '{text}' matches several users; use their e-mail")
            uid = matches[0] if matches else None
        if uid is None:
            raise RowError(f"Owner '{text}' is not a user in this workspace")
        if not self.scope.allows(uid):
            raise RowError(f"Owner '{text}' is outside your team; you can only assign to yourself or your team")
        return uid

    def _contact_email(self, vals: Dict[str, Any]) -> str:
        raw = vals.pop("email", None)
        email = (email_address(raw) or "").strip()
        if not email or not _EMAIL_RE.match(email):
            raise RowError("Missing or invalid email" if not email else f"Invalid email '{email}'")
        return email

    def _names(self, vals: Dict[str, Any]) -> None:
        full = vals.pop("full_name", None)
        if full and not (vals.get("first_name") or vals.get("last_name")):
            vals["first_name"], vals["last_name"] = split_name(full)

    def _choices(self, vals: Dict[str, Any]) -> None:
        for name, choices, label in (("lifecycle_stage", crm.LIFECYCLE_STAGES, "Lifecycle stage"),
                                     ("lead_status", crm.LEAD_STATUSES, "Lead status"),
                                     ("legal_basis", crm.LEGAL_BASES, "Legal basis")):
            if name in vals:
                vals[name] = pick(vals[name], choices, label)
        if "tags" in vals:
            vals["tags"] = normalize_tags(re.split(r"[;,]", vals["tags"] or "")) or None
        if "owner_email" in vals:
            owner = vals.pop("owner_email")
            if owner:
                vals["owner_id"] = self.owner_id_for(owner)
        for k in ("phone", "mobile_phone"):
            if vals.get(k):
                vals[k] = (clean_phone(vals[k]) or "")[:50] or None

    def custom_values(self, row: Row) -> Dict[str, Any]:
        custom: Dict[str, Any] = {}
        for column, target in self.plan.resolved.items():
            if not target.startswith("custom."):
                continue
            value = row.get(column)
            if value is None or str(value).strip() == "":
                continue
            key = target[len("custom."):]
            definition = self.plan.custom_defs[key]
            try:
                parsed = custom_value(definition.field_type, definition.label, definition.options or [],
                                      str(value).strip(), self.options.date_order)
                custom[key] = clean_custom_value(definition, parsed)
            except ValueProblem as exc:
                raise RowError(str(exc)) from exc
            except DomainError as exc:
                raise RowError(str(exc.detail)) from exc
        return custom

    def contact_values(self, row: Row) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        vals: Dict[str, Any] = self.values_for(row, "contact")
        email = self._contact_email(vals)
        self._names(vals)
        self._choices(vals)
        check_lengths({"email": email, **vals}, CONTACT_LIMITS, CONTACT_TARGETS)
        return email, vals, self.custom_values(row)

    # ── companies ──
    def _find_company(self, domain: Optional[str], name: Optional[str]) -> Optional[Account]:
        account = self.repo.live_account(self.tenant_id, domain=domain) if domain else None
        if not account and name:
            account = self.repo.live_account(self.tenant_id, name=name)
            if account and domain and account.domain and account.domain != domain:
                account = None
        return account

    def _create_company(self, domain: Optional[str], name: Optional[str]) -> Tuple[Optional[Account], bool]:
        if domain and self.repo.deleted_account_holds_domain(self.tenant_id, domain):
            return None, False  # the company is in the recycle bin: don't link to it or recreate it
        new_name = name or crm._name_from_domain(domain or "")
        if self.repo.account_name_taken(self.tenant_id, new_name):
            new_name = f"{new_name} ({domain})" if domain else f"{new_name} ({uuid.uuid4().hex[:4]})"
        account = Account(tenant_id=self.tenant_id, name=new_name[:255], domain=domain, lifecycle_stage="LEAD",
                          website=f"https://{domain}" if domain else None, owner_id=self.options.owner_id)
        try:
            return self.repo.insert(account), True
        except DuplicateRow:  # another import created it a moment ago (locking read sees it)
            existing = self.repo.lock_account(self.tenant_id, domain=domain, name=new_name)
            if not existing:
                raise RowError("Company could not be saved (name or domain clash)") from None
            return (None, False) if existing.deleted_at is not None else (existing, False)

    def _company_field(self, field_name: str, value: str) -> Any:
        if field_name != "annual_revenue":
            return value
        try:
            return parse_number(value)
        except ValueError:
            raise RowError(f"Annual revenue '{value}' is not a number") from None

    def _apply_company(self, account: Account, cvals: Dict[str, Any], domain: Optional[str], created: bool) -> None:
        overwrite = created or self.options.update_existing
        before = crm.snapshot(account, COMPANY_TARGETS)
        if domain and not account.domain:
            account.domain = domain
        for name, value in cvals.items():
            if value is None or name in ("name", "domain"):
                continue
            value = self._company_field(name, value)
            if overwrite or getattr(account, name) in (None, ""):
                setattr(account, name, value)
        tally = self.state.tally
        if created:
            tally.companies_created += 1
            self.repo.add(PropertyChange(tenant_id=self.tenant_id, object_type="COMPANY", object_id=account.account_id,
                                         field="created", new_value=f"Imported from {self.file_name}", source="IMPORT",
                                         changed_by=self.user.user_id))
            return
        if self.options.update_existing and account.account_id not in self.state.companies_touched:
            tally.companies_updated += 1
        crm.record_changes(self.uow.session, self.tenant_id, "COMPANY", account.account_id, before,
                           crm.snapshot(account, COMPANY_TARGETS), self.user.user_id, "IMPORT")

    def company_for_row(self, row: Row, contact_email: Optional[str]) -> Optional[Account]:
        cvals = self.values_for(row, "company")
        check_lengths(cvals, COMPANY_LIMITS, COMPANY_TARGETS)
        domain = company_domain(cvals, contact_email)
        name = cvals.get("name")
        if not domain and not name:
            return None
        account = self._find_company(domain, name)
        created = False
        if not account:
            account, created = self._create_company(domain, name)
            if account is None:
                return None
        if not created and account.owner_id and not self.scope.allows(account.owner_id):
            return account  # link to a company outside the importer's team, but don't edit it (BR-SH-02)
        self._apply_company(account, cvals, domain, created)
        self.state.companies_touched.add(account.account_id)
        return account

    # ── contacts ──
    def _apply_fields(self, p: Prospect, vals: Dict[str, Any], created: bool, overwrite: bool) -> None:
        for name, value in vals.items():
            if value is None:
                continue
            if name == "lifecycle_stage" and not created and not crm.lifecycle_move_allowed(p.lifecycle_stage, value, self.user):
                continue  # never move a stage backwards on import
            if overwrite or getattr(p, name) in (None, "", []):
                setattr(p, name, value)

    @staticmethod
    def _apply_custom(p: Prospect, custom: Dict[str, Any], overwrite: bool) -> None:
        if not custom:
            return
        merged = dict(p.custom_fields or {})
        for k, v in custom.items():
            if v is not None and (overwrite or merged.get(k) in (None, "", [])):
                merged[k] = v
        p.custom_fields = merged or None

    def apply_contact(self, p: Prospect, vals: Dict[str, Any], custom: Dict[str, Any],
                      account: Optional[Account], created: bool) -> None:
        overwrite = created or self.options.update_existing
        fields = tuple(f for f in crm.CONTACT_FIELDS if hasattr(Prospect, f)) + ("tags", "custom_fields")
        before = crm.snapshot(p, fields)
        self._apply_fields(p, vals, created, overwrite)
        self._apply_custom(p, custom, overwrite)
        if vals.get("poc_state") and overwrite:
            p.timezone = get_timezone_for_state(p.poc_state)
        if account and (overwrite or not p.account_id):
            p.account_id = account.account_id
            p.company_name = p.company_name if (p.company_name and not overwrite) else account.name
        if not created:
            crm.record_changes(self.uow.session, self.tenant_id, "CONTACT", p.prospect_id, before,
                               crm.snapshot(p, fields), self.user.user_id, "IMPORT")

    def _missing_required(self, custom: Dict[str, Any]) -> List[str]:
        return [d.label for d in self.plan.required_defs if custom.get(d.field_key) in (None, "", [])]

    def new_contact(self, email: str, vals: Dict[str, Any]) -> Prospect:
        domain = crm.email_domain(email)
        return Prospect(prospect_id=str(uuid.uuid4()), tenant_id=self.tenant_id, email=email,
                        email_type="PERSONAL" if crm.is_personal_domain(domain) else "BUSINESS",
                        email_provider=domain, consent_source="IMPORT", consent_timestamp=utcnow(),
                        is_valid_email=True,
                        consent_status="UNSUBSCRIBED" if email.lower() in self.unsubscribed else "OPT_IN",
                        owner_id=vals.get("owner_id") or self.options.owner_id or self.user.user_id,
                        lifecycle_stage="LEAD", lead_status="NEW")

    def create_contact(self, email: str, vals: Dict[str, Any], custom: Dict[str, Any],
                       account: Optional[Account]) -> Prospect:
        """Insert a new contact; if another import inserted it a moment ago, update that one."""
        p = self.new_contact(email, vals)
        if self.options.default_source and not vals.get("lead_source"):
            vals = {**vals, "lead_source": self.options.default_source}
        self.apply_contact(p, vals, custom, account, created=True)
        missing = self._missing_required(p.custom_fields or {})
        if missing:  # same rule as creating a contact in the app (BR-CM-08)
            raise RowError(f"Missing required field: {', '.join(missing)}")
        try:
            self.repo.insert(p)
        except DuplicateRow:
            # Created by another import in the meantime: update it instead (BR-CM-28)
            existing = self.repo.lock_contact(self.tenant_id, email)
            if existing is None or existing.deleted_at is not None:
                raise RowError("Could not save contact") from None
            if not self.scope.allows(existing.owner_id):
                raise RowError(NOT_VISIBLE) from None
            self.apply_contact(existing, vals, custom, account, created=False)
            self.state.tally.contacts_updated += 1
            return existing
        self.state.tally.contacts_created += 1
        self.repo.add(PropertyChange(tenant_id=self.tenant_id, object_type="CONTACT", object_id=p.prospect_id,
                                     field="created", new_value=f"Imported from {self.file_name}",
                                     source="IMPORT", changed_by=self.user.user_id))
        return p

    def _skip_for_mode(self, exists: bool) -> bool:
        """CREATE skips rows matching an existing record; UPDATE_ONLY skips rows matching none."""
        return not (self.options.mode.updates if exists else self.options.mode.creates)

    def _check_existing(self, p: Optional[Prospect]) -> None:
        if p is not None and p.deleted_at is not None:
            raise RowError("Matches a deleted contact; restore it first")
        if p is not None and not self.scope.allows(p.owner_id):
            # Never overwrite or reassign a colleague's contact outside your team (BR-SH-02)
            raise RowError(NOT_VISIBLE)

    def save_contact_row(self, row: Row, email: str, vals: Dict[str, Any], custom: Dict[str, Any],
                         existing: Dict[str, Prospect]) -> None:
        """One contact row in its own savepoint: a RowError or refused value undoes only this row."""
        p = existing.get(email.lower())
        self._check_existing(p)
        if self._skip_for_mode(p is not None):
            self.state.tally.skipped += 1
            return
        with self.repo.row():
            account = self.company_for_row(row, email)  # from columns, else the email domain (BR-CM-04)
            if p is None:
                p = self.create_contact(email, vals, custom, account)
            else:
                if self.options.owner_id and self.options.update_existing and "owner_id" not in vals:
                    vals["owner_id"] = self.options.owner_id
                self.apply_contact(p, vals, custom, account, created=False)
                if self.options.update_existing:
                    self.state.tally.contacts_updated += 1
            self.state.list_members.append(p.prospect_id)

    def save_company_row(self, row: Row) -> None:
        cvals = self.values_for(row, "company")
        if not self.options.mode.creates or not self.options.mode.updates:
            exists = self._find_company(company_domain(cvals, None), cvals.get("name")) is not None
            if self._skip_for_mode(exists):
                self.state.tally.skipped += 1
                return
        with self.repo.row():
            if not self.company_for_row(row, None):
                raise RowError("Missing company name or domain")

    def guarded(self, row_no: int, row: Row, save: Callable[..., None], *args: Any) -> None:
        """Run one row's save; a refusal or a value the database rejects becomes that row's error."""
        try:
            save(row, *args)
        except RowError as exc:
            self.state.errors.append(error_entry(row_no, str(exc), row))
        except RejectedValue as exc:
            self.state.errors.append(error_entry(row_no, data_error_reason(exc, ALL_LABELS), row))

    def parse_contacts(self, chunk: Chunk) -> List[Tuple[int, Row, str, Dict[str, Any], Dict[str, Any]]]:
        parsed = []
        for row_no, row in chunk:
            try:
                email, vals, custom = self.contact_values(row)
                key = email.lower()
                if key in self.state.seen_emails:
                    raise RowError(f"Duplicate of row {self.state.seen_emails[key]} in this file")
                self.state.seen_emails[key] = row_no
                parsed.append((row_no, row, email, vals, custom))
            except RowError as exc:
                self.state.errors.append(error_entry(row_no, str(exc), row))
        return parsed

    def process_chunk(self, chunk: Chunk) -> None:
        """One all-or-nothing unit; the caller commits, or rolls back and retries on deadlock."""
        if not self.plan.has_contacts:
            for row_no, row in chunk:
                self.guarded(row_no, row, self.save_company_row)
            return
        parsed = self.parse_contacts(chunk)
        existing = self.repo.contacts_by_email(self.tenant_id, [e for _, _, e, _, _ in parsed]) if parsed else {}
        for row_no, row, email, vals, custom in parsed:
            self.guarded(row_no, row, self.save_contact_row, email, vals, custom, existing)

    def add_list_members(self, list_id: str, since: int) -> None:
        """This chunk's contacts join the import's static list, in the chunk's transaction."""
        ids = list(dict.fromkeys(self.state.list_members[since:]))
        if not ids:
            return
        present = self.repo.list_member_ids(list_id, ids)
        for pid in ids:
            if pid not in present:
                self.repo.add(ProspectListMember(list_id=list_id, prospect_id=pid, is_new_prospect=True))

    def remember_emails(self, rows: Sequence[Tuple[int, Row]]) -> None:
        """Rebuild the duplicate check after a resume: e-mails of the rows already processed."""
        for row_no, row in rows:
            try:
                email = self._contact_email(self.values_for(row, "contact"))
            except RowError:
                continue
            self.state.seen_emails.setdefault(email.lower(), row_no)

    # ── dry run ──
    def evaluate_chunk(self, chunk: Chunk) -> None:
        """Count what the chunk would do, without writing anything."""
        if not self.plan.has_contacts:
            for row_no, row in chunk:
                self._evaluate_company(row_no, row)
            return
        parsed = self.parse_contacts(chunk)
        existing = self.repo.contacts_by_email(self.tenant_id, [e for _, _, e, _, _ in parsed]) if parsed else {}
        for row_no, row, email, _vals, custom in parsed:
            try:
                self._evaluate_contact(row, email, custom, existing.get(email.lower()))
            except RowError as exc:
                self.state.errors.append(error_entry(row_no, str(exc), row))

    def _evaluate_contact(self, row: Row, email: str, custom: Dict[str, Any], p: Optional[Prospect]) -> None:
        self._check_existing(p)
        cvals = self.values_for(row, "company")
        check_lengths(cvals, COMPANY_LIMITS, COMPANY_TARGETS)
        company_domain(cvals, email)
        for name, value in cvals.items():
            if value is not None:
                self._company_field(name, value)
        tally = self.state.tally
        if self._skip_for_mode(p is not None):
            tally.skipped += 1
        elif p is None:
            missing = self._missing_required(custom)
            if missing:
                raise RowError(f"Missing required field: {', '.join(missing)}")
            tally.contacts_created += 1
        elif self.options.update_existing:
            tally.contacts_updated += 1

    def _evaluate_company(self, row_no: int, row: Row) -> None:
        try:
            cvals = self.values_for(row, "company")
            check_lengths(cvals, COMPANY_LIMITS, COMPANY_TARGETS)
            domain = company_domain(cvals, None)
            if not domain and not cvals.get("name"):
                raise RowError("Missing company name or domain")
            for name, value in cvals.items():
                if value is not None:
                    self._company_field(name, value)
            found = self._find_company(domain, cvals.get("name"))
        except RowError as exc:
            self.state.errors.append(error_entry(row_no, str(exc), row))
            return
        tally = self.state.tally
        if self._skip_for_mode(found is not None):
            tally.skipped += 1
        elif found is None:
            tally.companies_created += 1
        else:
            tally.companies_updated += 1


def _owners_by_name(users: Sequence[User]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for u in users:
        name = " ".join(f"{u.first_name or ''} {u.last_name or ''}".lower().split())
        if name:
            out.setdefault(name, []).append(u.user_id)
    return out
