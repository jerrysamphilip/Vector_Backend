# app/services/prospect_upload_service.py

from typing import Any, Dict, List, Mapping, Optional, Tuple
import re
import uuid
import math
from datetime import timedelta
from sqlalchemy.orm import Session
from sqlalchemy import case, func
from sqlalchemy.exc import DataError, IntegrityError
from app.repositories.lists import free_list_name
from app.core.clock import utcnow
from app.core.errors import ConflictError

from app.models import (
    Prospect,
    ProspectList,
    ProspectListMember,
    GlobalUnsubscribe,
)
from app.services.contact_service import get_or_create_account
from app.services.import_limits import column_limits, oversized_fields
from app.utils.email_utils import parse_email
from app.utils.business_calendar import get_timezone_for_state

# Row fields as the upload screen names them (spreadsheet headers)
FIELD_LABELS = {
    "email": "Email",
    "first_name": "First Name",
    "last_name": "Last Name",
    "company_name": "Company Name",
    "designation": "Designation",
    "industry": "Industry",
    "emp_band": "Emp Band",
    "linkedin_url": "POC LinkedIn",
    "phone": "Phone",
    "mobile_phone": "Mobile",
    "poc_state": "POC State",
    "poc_city": "POC City",
    "poc_country": "POC Country",
}
REQUIRED_FIELDS = ("first_name", "last_name", "company_name")
# Column lengths of the prospects table, so an over-long value is this row's problem, not a
# failed upload (MySQL strict mode rejects the whole statement)
PROSPECT_LIMITS = column_limits(Prospect, FIELD_LABELS)

# SES refuses an address whose local part has whitespace / control characters or whose domain
# has anything but ASCII letter/digit/hyphen labels ("Local address contains control or
# whitespace", "Domain contains illegal character"): caught here instead of as a FAILED send.
_SES_LOCAL_PART = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_SES_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def is_sendable_address(email: Optional[str]) -> bool:
    """True when SES can deliver to the address (ASCII, no whitespace or control characters)."""
    if not email or not isinstance(email, str) or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    labels = domain.split(".")
    return (
        bool(_SES_LOCAL_PART.fullmatch(local))
        and len(domain) <= 253
        and len(labels) >= 2
        and all(_SES_DOMAIN_LABEL.fullmatch(label) for label in labels)
    )


def field_problems(values: Mapping[str, Any]) -> List[str]:
    """Problems beyond the required-field checks: an address SES cannot send to, control
    characters in names (they break email headers and greetings) and values longer than their column."""
    problems = []
    email = values.get("email")
    if isinstance(email, str) and email.strip() and parse_email(email) and not is_sendable_address(email.strip()):
        problems.append("Email contains spaces or characters that cannot be delivered to")
    problems.extend(
        f"{FIELD_LABELS[field]} contains control characters"
        for field in REQUIRED_FIELDS + ("designation",)
        if isinstance(values.get(field), str) and _CONTROL_CHARS.search(str(values.get(field)))
    )
    problems.extend(oversized_fields({f: values.get(f) for f in PROSPECT_LIMITS}, PROSPECT_LIMITS, FIELD_LABELS))
    return problems


def _sanitize_value(val) -> Optional[str]:
    """Convert pandas NaN/None/empty to None for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    if isinstance(val, str):
        stripped = val.strip()
        return stripped if stripped else None
    return str(val).strip() or None


# STEP 0: DRY RUN (VALIDATION ONLY)
# =====================================================

# Accepted spreadsheet headers for phone numbers (first non-blank wins)
PHONE_COLUMNS = ("Phone", "Phone Number", "POC Phone", "Direct Phone", "Work Phone", "Office Phone")
MOBILE_COLUMNS = ("Mobile", "Mobile Phone", "Mobile Number", "Cell", "Cell Phone")


def _first_value(row, columns) -> Optional[str]:
    for column in columns:
        value = _sanitize_value(row.get(column))
        if value:
            return str(value)[:50]
    return None


def dry_run_validate(
    df,
    db: Session,
    tenant_id: str,
    uploaded_by: str,
    file_name: str,
) -> Tuple[str, Dict[str, Any]]:
    records = []
    accepted_count = 0
    rejected_count = 0
    errors_summary = []

    # Normalize column names (strip whitespace to handle "POC LinkedIn " etc.)
    df.columns = df.columns.str.strip()

    # Track seen emails for duplicate detection
    seen_emails = set()

    unsubscribed = {
        email: reason
        for email, reason in db.query(GlobalUnsubscribe.email, GlobalUnsubscribe.reason)
        .filter(GlobalUnsubscribe.tenant_id == tenant_id)
        .all()
    }

    for idx, row in df.iterrows():
        row_num = idx + 1
        errors = []

        email_info = parse_email(row.get("email"))

        email = email_info.get("email") if email_info else None
        email_type = email_info.get("email_type") if email_info else None
        email_provider = email_info.get("email_provider") if email_info else None

        # Parse POC State, POC City, POC Country, and Emp Band
        poc_state = _sanitize_value(row.get("POC State"))
        poc_city = _sanitize_value(row.get("POC City"))
        poc_country = _sanitize_value(row.get("POC Country") or row.get("Country"))
        emp_band = _sanitize_value(row.get("Emp Band"))
        phone = _first_value(row, PHONE_COLUMNS)
        mobile_phone = _first_value(row, MOBILE_COLUMNS)

        # Required fields: First Name, Last Name, Company Name, Email.
        # Everything else (POC State/City/Country, Designation, Industry, Emp Band,
        # LinkedIn, etc.) may be blank without rejecting the row.
        first_name = _sanitize_value(row.get("First Name"))
        last_name = _sanitize_value(row.get("Last Name"))
        company_name = _sanitize_value(row.get("Company Name"))

        # Auto-derive timezone from POC State
        timezone = get_timezone_for_state(state=poc_state) if poc_state else None

        # Validation checks
        if not email:
            errors.append("Invalid or personal email")

        if not first_name:
            errors.append("Missing required field: First Name")

        if not last_name:
            errors.append("Missing required field: Last Name")

        if not company_name:
            errors.append("Missing required field: Company Name")

        # Check for duplicate emails in the uploaded file
        if email and email in seen_emails:
            errors.append("Duplicate email in file - first occurrence kept")

        if email and email in unsubscribed:
            reason = unsubscribed.get(email)
            errors.append(f"Globally unsubscribed ({reason})" if reason else "Globally unsubscribed")

        errors.extend(field_problems({
            "email": email or _sanitize_value(row.get("email")), "first_name": first_name,
            "last_name": last_name, "company_name": company_name,
            "designation": _sanitize_value(row.get("Designation")), "industry": _sanitize_value(row.get("Industry")),
            "emp_band": emp_band, "linkedin_url": _sanitize_value(row.get("POC LinkedIn")),
            "poc_state": poc_state, "poc_city": poc_city, "poc_country": poc_country,
        }))

        if errors:
            rejected_count += 1
            errors_summary.append({
                "row": row_num,
                "errors": errors,
            })
            records.append({
                "row": row_num,
                "email": _sanitize_value(row.get("email")),
                "first_name": first_name,
                "last_name": last_name,
                "company_name": company_name,
                "designation": _sanitize_value(row.get("Designation")),
                "industry": _sanitize_value(row.get("Industry")),
                "emp_band": emp_band,
                "linkedin_url": _sanitize_value(row.get("POC LinkedIn")),
                "phone": phone,
                "mobile_phone": mobile_phone,
                "poc_state": poc_state,
                "poc_city": poc_city,
                "poc_country": poc_country,
                "timezone": timezone,
                "email_type": email_type,
                "email_provider": email_provider,
                "status": "REJECTED",
                "reason": ", ".join(errors),
            })
        else:
            # Mark email as seen (only if accepted)
            if email:
                seen_emails.add(email)
            accepted_count += 1
            records.append({
                "row": row_num,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "company_name": company_name,
                "designation": _sanitize_value(row.get("Designation")),
                "industry": _sanitize_value(row.get("Industry")),
                "emp_band": emp_band,
                "linkedin_url": _sanitize_value(row.get("POC LinkedIn")),
                "phone": phone,
                "mobile_phone": mobile_phone,
                "poc_state": poc_state,
                "poc_city": poc_city,
                "poc_country": poc_country,
                "timezone": timezone,
                "email_type": email_type,
                "email_provider": email_provider,
                "status": "ACCEPTED",
                "reason": None,
            })

    upload_id = str(uuid.uuid4())

    return upload_id, {
        "total_rows": len(records),
        "accepted": accepted_count,
        "rejected": rejected_count,
        "records": records,
        "errors": errors_summary,
    }


# =====================================================
# STEP 0.5: RE-VALIDATE EDITED RECORDS
# =====================================================

def revalidate_prospect_records(
    records: list,
    db: Session,
    tenant_id: str,
) -> Dict[str, Any]:
    """
    Re-run validation on edited records.
    Handles all edge cases:
    - Re-parses email (catches personal email edits)
    - Checks First Name, Last Name, Company Name are present
    - Checks not globally unsubscribed
    - Checks no duplicates in batch
    - Checks if email already exists in DB (warning only)
    POC State/City/Country are optional — left blank if not provided.
    """
    validated = []
    accepted_count = 0
    rejected_count = 0
    seen_emails = set()

    # Get unsubscribed emails (lower-cased) with their suppression reason
    unsubscribed_lower = {
        email.lower(): reason
        for email, reason in db.query(GlobalUnsubscribe.email, GlobalUnsubscribe.reason)
        .filter(GlobalUnsubscribe.tenant_id == tenant_id)
        .all()
    }

    for r in records:
        errors = []
        row_num = r.get("row", 0)

        # Re-parse email (catches personal email edits)
        raw_email = r.get("email")
        email_info = parse_email(raw_email)
        
        email = email_info.get("email") if email_info else None
        email_type = email_info.get("email_type") if email_info else None
        email_provider = email_info.get("email_provider") if email_info else None

        if not email:
            errors.append("Invalid or personal email")

        # Required fields: First Name, Last Name, Company Name, Email.
        # POC State/City/Country may be blank without rejecting the row.
        first_name = _sanitize_value(r.get("first_name"))
        last_name = _sanitize_value(r.get("last_name"))
        company_name = _sanitize_value(r.get("company_name"))

        if not first_name:
            errors.append("Missing required field: First Name")

        if not last_name:
            errors.append("Missing required field: Last Name")

        if not company_name:
            errors.append("Missing required field: Company Name")

        # POC State (not required — pass through / normalize if present)
        poc_state = r.get("poc_state")
        poc_city = r.get("poc_city")
        if poc_state and str(poc_state).strip():
            poc_state = str(poc_state).strip()
        else:
            poc_state = None

        # Check POC Country
        poc_country = r.get("poc_country")
        if poc_country:
            poc_country = str(poc_country).strip()

        # Pass through emp_band
        emp_band = r.get("emp_band")

        # Check globally unsubscribed
        email_lower = email.lower() if email else None

        if email_lower and email_lower in unsubscribed_lower:
            reason = unsubscribed_lower.get(email_lower)
            errors.append(f"Globally unsubscribed ({reason})" if reason else "Globally unsubscribed")

        # Check duplicate in batch
        if email_lower and email_lower in seen_emails:
            errors.append("Duplicate email in batch")
        
        if email_lower:
            seen_emails.add(email_lower)

        errors.extend(field_problems({**r, "email": raw_email}))

        # Auto-derive timezone
        timezone = get_timezone_for_state(poc_state) if poc_state else None

        # Determine status
        if errors:
            rejected_count += 1
            validated.append({
                **r,
                "email": raw_email,
                "email_type": email_type,
                "email_provider": email_provider,
                "poc_state": poc_state,
                "poc_city": poc_city,
                "poc_country": poc_country,
                "timezone": timezone,
                "status": "REJECTED",
                "reason": ", ".join(errors),
            })
        else:
            accepted_count += 1
            validated.append({
                **r,
                "email": email,
                "email_type": email_type,
                "email_provider": email_provider,
                "poc_state": poc_state,
                "poc_city": poc_city,
                "poc_country": poc_country,
                "timezone": timezone,
                "status": "ACCEPTED",
                "reason": None,
            })

    return {
        "total_rows": len(validated),
        "accepted": accepted_count,
        "rejected": rejected_count,
        "records": validated,
    }


# =====================================================
# STEP 1–3: CONFIRM UPLOAD (MASTER + SNAPSHOT)
# =====================================================

# Fields copied onto an existing contact when a re-upload carries newer values
_UPDATABLE_FIELDS = (
    "first_name", "last_name", "company_name", "designation", "industry",
    "emp_band", "linkedin_url", "poc_state", "poc_city", "poc_country",
)
# Rows saved per savepoint; a batch that fails is retried row by row to find the bad row
SAVE_BATCH = 200


def _row_order(entry: dict) -> Tuple[int, int]:
    row = entry.get("row")
    return (0, row) if isinstance(row, int) else (1, 0)


def _not_added(record: dict, reason: str) -> dict:
    return {"row": record.get("row"), "email": record.get("email"), "reason": reason}


def recheck_accepted(record: dict) -> Tuple[Optional[dict], Optional[str]]:
    """
    Run the validation rules again on a row the client sent as ACCEPTED, so a row edited after
    validation (or a hand-made request) cannot skip them: email format and personal-email policy,
    characters SES cannot send, required names and column lengths. Returns (row to save with the
    email normalised the way validation does, None) or (None, reason in the validation wording).
    """
    raw_email = _sanitize_value(record.get("email"))
    email_info = parse_email(raw_email)
    email = email_info.get("email") if email_info else None

    problems = []
    if not raw_email:
        problems.append("Missing email address")
    elif not email:
        problems.append("Invalid or personal email")
    for field in REQUIRED_FIELDS:
        if not _sanitize_value(record.get(field)):
            problems.append(f"Missing required field: {FIELD_LABELS[field]}")
    problems.extend(field_problems({**record, "email": email or raw_email}))

    if problems or not email_info:
        return None, ", ".join(problems) or "Invalid or personal email"
    return {
        **record,
        "email": email,
        "email_type": email_info.get("email_type"),
        "email_provider": email_info.get("email_provider"),
    }, None


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, IntegrityError):
        return "Conflicts with an existing contact (duplicate email address)"
    return "A value is too long or not valid for the database"


class _Saver:
    """Saves one upload's rows. Caches (contacts by email, accounts by name) change only after the
    savepoint holding a batch is released, so a rolled-back batch leaves nothing stale behind."""

    def __init__(self, db: Session, tenant_id: str, uploaded_by: str, existing_by_email: Dict[str, Prospect]):
        self.db, self.tenant_id, self.uploaded_by = db, tenant_id, uploaded_by
        self.existing_by_email = existing_by_email
        self.accounts_by_name: Dict[str, Optional[str]] = {}
        self._pending_accounts: Dict[str, Optional[str]] = {}

    def _account_id_for(self, company_name: Optional[str], record: dict) -> Optional[str]:
        key = (company_name or "").strip().lower()
        if not key:
            return None
        if key in self.accounts_by_name:
            return self.accounts_by_name[key]
        if key not in self._pending_accounts:
            account = get_or_create_account(
                self.db, self.tenant_id, company_name,
                industry=record.get("industry"), emp_band=record.get("emp_band"))
            self._pending_accounts[key] = account.account_id if account else None
        return self._pending_accounts[key]

    def _update(self, existing: Prospect, record: dict) -> Tuple[str, bool]:
        for field in _UPDATABLE_FIELDS:
            setattr(existing, field, record.get(field) or getattr(existing, field))
        existing.phone = existing.phone or record.get("phone")
        existing.mobile_phone = existing.mobile_phone or record.get("mobile_phone")
        if not existing.account_id:
            existing.account_id = self._account_id_for(existing.company_name, record)
        if not existing.owner_id:
            existing.owner_id = self.uploaded_by
        # Re-derive timezone from potentially updated poc_state
        if record.get("poc_state"):
            existing.timezone = get_timezone_for_state(record.get("poc_state"))
        return existing.prospect_id, False

    def _insert(self, record: dict) -> Tuple[str, bool]:
        prospect_id = str(uuid.uuid4())
        # Auto-derive timezone from poc_state (ensures correct timezone even after user edits)
        poc_state = record.get("poc_state")
        self.db.add(Prospect(
            prospect_id=prospect_id,
            tenant_id=self.tenant_id,
            first_name=record.get("first_name"),
            last_name=record.get("last_name"),
            email=record["email"],
            email_type=record.get("email_type"),
            email_provider=record.get("email_provider"),
            company_name=record.get("company_name"),
            designation=record.get("designation"),
            industry=record.get("industry"),
            emp_band=record.get("emp_band"),
            linkedin_url=record.get("linkedin_url"),
            poc_state=poc_state,
            poc_city=record.get("poc_city"),
            poc_country=record.get("poc_country"),
            timezone=get_timezone_for_state(poc_state) if poc_state else None,
            consent_status="OPT_IN",
            is_valid_email=True,
            phone=record.get("phone"),
            mobile_phone=record.get("mobile_phone"),
            owner_id=self.uploaded_by,
            account_id=self._account_id_for(record.get("company_name"), record),
        ))
        return prospect_id, True

    def save_batch(self, batch: List[dict]) -> List[Tuple[str, bool]]:
        """Upsert the batch in one savepoint; raises (and rolls the savepoint back) on a bad row."""
        self._pending_accounts = {}
        with self.db.begin_nested():
            outcomes = []
            for record in batch:
                existing = self.existing_by_email.get(record["email"].lower())
                outcomes.append(self._update(existing, record) if existing else self._insert(record))
            self.db.flush()
        self.accounts_by_name.update(self._pending_accounts)
        return outcomes

    def save_all(self, records: List[dict]) -> Tuple[List[Tuple[dict, Tuple[str, bool]]], List[dict]]:
        """(saved (record, (prospect_id, is_new)) pairs, not_added entries for rows the database refused)."""
        saved: List[Tuple[dict, Tuple[str, bool]]] = []
        failed: List[dict] = []
        for start in range(0, len(records), SAVE_BATCH):
            batch = records[start:start + SAVE_BATCH]
            try:
                saved.extend(zip(batch, self.save_batch(batch)))
            except (IntegrityError, DataError):
                for record in batch:  # find the bad row(s); the others still save
                    try:
                        saved.extend(zip([record], self.save_batch([record])))
                    except (IntegrityError, DataError) as exc:
                        failed.append(_not_added(record, _failure_reason(exc)))
        return saved, failed


def stored_upload_result(db: Session, upload_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """Result of an upload that was already confirmed, so confirming it again saves nothing twice."""
    exists = db.query(ProspectList.list_id).filter(
        ProspectList.list_id == upload_id,
        ProspectList.tenant_id == tenant_id,
    ).first()
    if not exists:
        return None
    total, new = db.query(
        func.count(ProspectListMember.id),
        func.coalesce(func.sum(case((ProspectListMember.is_new_prospect.is_(True), 1), else_=0)), 0),
    ).filter(ProspectListMember.list_id == upload_id).one()
    total, new = int(total or 0), int(new or 0)
    return {
        "list_id": upload_id,
        "inserted": total,
        "new_prospects": new,
        "updated": total - new,
        "skipped_unsubscribed": 0,
        "rejected": 0,
        "failed": 0,
        "not_added": [],
        "status": "CONFIRMED",
        "already_confirmed": True,
    }


def _dedupe_by_email(records: List[dict]) -> List[dict]:
    """Collapse same-email rows (e.g. an edited row that now collides with another accepted row),
    so one batch never inserts two contacts with the same (tenant_id, email). Last occurrence wins,
    since edits are appended/overwritten in place."""
    by_email: Dict[str, dict] = {}
    for r in records:
        by_email[r["email"].lower()] = r
    return list(by_email.values())


def _split_unsubscribed(db: Session, tenant_id: str, records: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Loophole #3: re-check the unsubscribe list at save time (race with validation)."""
    unsubscribed = {
        e[0].lower()
        for e in db.query(GlobalUnsubscribe.email)
        .filter(GlobalUnsubscribe.tenant_id == tenant_id)
        .all()
    }
    keep = [r for r in records if r["email"].lower() not in unsubscribed]
    skipped = [_not_added(r, "Globally unsubscribed") for r in records if r["email"].lower() in unsubscribed]
    return keep, skipped


def _add_list(db: Session, upload_id: str, title: str, uploaded_by: str, tenant_id: str,
              saved: List[Tuple[dict, Tuple[str, bool]]]) -> None:
    db.add(ProspectList(list_id=upload_id, tenant_id=tenant_id, list_name=free_list_name(db, tenant_id, title),
                        source_type="UPLOAD", uploaded_by=uploaded_by))
    base_added_at = utcnow()
    for idx, (_, (prospect_id, is_new)) in enumerate(saved):
        db.add(ProspectListMember(
            id=str(uuid.uuid4()),
            list_id=upload_id,
            prospect_id=prospect_id,
            is_new_prospect=is_new,
            # Preserve original upload row order for deterministic list display.
            # One second apart: readable in exports and unambiguous at any precision.
            added_at=base_added_at + timedelta(seconds=idx),
        ))


def _commit_list(db: Session, upload_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """Commit; when a concurrent confirmation of the same upload won, return its stored result."""
    try:
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        stored = stored_upload_result(db, upload_id, tenant_id)
        if stored:
            return stored
        raise ConflictError("This upload id is already in use. Please validate the file again.")


def confirm_upload(
    upload_id: str,
    title: str,
    records: list,
    db: Session,
    tenant_id: str,
    uploaded_by: str,
) -> Dict[str, Any]:
    """
    Master + snapshot ingestion.

    The client's ACCEPTED status is not trusted: every row is validated again. A row that fails,
    or that the database refuses, is listed in `not_added` with the reason and the other rows are
    saved. Confirming the same upload_id again returns the stored result instead of re-inserting.
    """
    stored = stored_upload_result(db, upload_id, tenant_id)
    if stored:
        return stored

    accepted = [r for r in records if isinstance(r, dict) and r.get("status") == "ACCEPTED"]
    if not accepted:
        return {"list_id": upload_id, "inserted": 0, "new_prospects": 0, "status": "NO_ACCEPTED_RECORDS"}

    not_added: List[dict] = []
    rechecked = []
    for r in accepted:
        row, problem = recheck_accepted(r)
        if row is None:
            not_added.append(_not_added(r, problem or "Invalid row"))
        else:
            rechecked.append(row)
    rejected = len(not_added)

    to_save, unsubscribed = _split_unsubscribed(db, tenant_id, _dedupe_by_email(rechecked))
    not_added.extend(unsubscribed)

    emails = [r["email"] for r in to_save]
    existing = db.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.email.in_(emails)).all() \
        if emails else []
    saver = _Saver(db, tenant_id, uploaded_by, {p.email.lower(): p for p in existing if p.email})
    try:
        saved, failed = saver.save_all(to_save)
        not_added.extend(failed)
        if saved:
            _add_list(db, upload_id, title, uploaded_by, tenant_id, saved)
            raced = _commit_list(db, upload_id, tenant_id)
            if raced:
                return raced
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise

    new_count = sum(1 for _, (_, is_new) in saved if is_new)
    return {
        "list_id": upload_id,
        "inserted": len(saved),
        "new_prospects": new_count,
        "updated": len(saved) - new_count,
        "skipped_unsubscribed": len(unsubscribed),
        "rejected": rejected,
        "failed": len(failed),
        "not_added": sorted(not_added, key=_row_order),
        "status": "CONFIRMED" if saved else "NO_RECORDS_ADDED",
    }
