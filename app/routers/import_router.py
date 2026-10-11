# app/routers/import_router.py
"""
CRM import (BR-CM-25..30): CSV / XLSX of contacts, companies, or both in one file.

1. POST /imports/preview  — headers, sample rows and a suggested column mapping
2. POST /imports          — run it with a mapping: {column: target}
     targets: "contact.<field>", "company.<field>", "custom.<key>",
              "new:<TYPE>:<Label>" (creates a custom property), or "skip"
3. GET  /imports, /imports/{id}, /imports/{id}/errors.csv — history and rejected rows

Contacts are matched by email (unique per workspace) and companies by domain, then name.
New contacts and companies are inserted under the database's unique keys, so several
users importing at once can't create duplicates: a collision becomes an update.
"""

import csv
import io
import json
import random
import re
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.database import get_db
from app.models.account import Account
from app.models.contact_field import FIELD_TYPES, ContactFieldDefinition
from app.models.crm import ImportJob, PropertyChange
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User
from app.services import crm
from app.services.contact_service import can_access_contact, can_manage_contacts, can_see_owner, normalize_tags

router = APIRouter(prefix="/imports", tags=["Imports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

MAX_ROWS = 10_000
MAX_FILE_MB = 20
CHUNK = 500
DEADLOCK_RETRIES = 5
NOT_VISIBLE = "Contact already exists, owned by someone outside your visibility"

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
_ALIASES = {
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
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RowError(Exception):
    pass


def _delimiter(text: str) -> str:
    """Comma, semicolon, tab or pipe; a one-column file has none, so default to comma
    (letting pandas guess from any character split a lone "Email" header on "m")."""
    try:
        return csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _read_file(upload: UploadFile) -> pd.DataFrame:
    raw = upload.file.read()
    if len(raw) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File is larger than {MAX_FILE_MB} MB")
    name = (upload.filename or "").lower()
    try:
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            df = pd.read_csv(io.StringIO(text), dtype=str, sep=_delimiter(text), engine="python")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read the file: {exc}")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    if len(df) > MAX_ROWS:
        raise HTTPException(status_code=400, detail=f"Files can have at most {MAX_ROWS:,} rows; this one has {len(df):,}")
    return df.astype(object).where(pd.notna(df), None)  # blank cells -> None (NaN isn't valid JSON)


def _suggest(headers: List[str], custom: Dict[str, str]) -> Dict[str, str]:
    taken, mapping = set(), {}
    for h in headers:
        key = re.sub(r"\s+", " ", h.strip().lower().replace("_", " "))
        target = next((t for t, names in _ALIASES.items() if key in names and t not in taken), None)
        if not target:
            target = next((f"custom.{k}" for k, label in custom.items()
                           if key in (label.lower(), k.replace("_", " ")) and f"custom.{k}" not in taken), None)
        mapping[h] = target or "skip"
        if target:
            taken.add(target)
    return mapping


def _pick(value, choices: Dict[str, str], name: str):
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    for code, label in choices.items():
        if v.upper().replace(" ", "_") == code or v.lower() == label.lower():
            return code
    raise RowError(f"{name} '{v}' is not one of: {', '.join(choices.values())}")


def _job_dict(job: ImportJob, names: Dict[str, str] = None) -> dict:
    return {
        "import_id": job.import_id, "file_name": job.file_name, "status": job.status,
        "created_by": job.created_by, "created_by_name": (names or {}).get(job.created_by),
        "total_rows": job.total_rows, "contacts_created": job.contacts_created,
        "contacts_updated": job.contacts_updated, "companies_created": job.companies_created,
        "companies_updated": job.companies_updated, "error_count": job.error_count,
        "message": job.message, "options": job.options, "mapping": job.mapping,
        "started_at": job.started_at, "finished_at": job.finished_at,
    }


@router.post("/preview")
def preview(file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    if not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")
    df = _read_file(file)
    custom = {d.field_key: d.label for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == current_user.tenant_id)}
    headers = list(df.columns)
    return {
        "file_name": file.filename, "row_count": len(df), "headers": headers,
        "sample": df.head(5).to_dict(orient="records"),
        "suggested_mapping": _suggest(headers, custom),
        "targets": {
            "contact": [{"value": f"contact.{k}", "label": v} for k, v in CONTACT_TARGETS.items()],
            "company": [{"value": f"company.{k}", "label": v} for k, v in COMPANY_TARGETS.items()],
            "custom": [{"value": f"custom.{k}", "label": v} for k, v in custom.items()],
            "new_types": list(FIELD_TYPES),
        },
    }


@router.post("", status_code=201)
def run_import(
    file: UploadFile = File(...),
    mapping: str = Form(..., description="JSON {column: target}"),
    options: str = Form("{}", description='JSON {"owner_id", "list_id", "new_list_name", "update_existing"}'),
    db: Session = Depends(get_db),
    current_user: User = Depends(tenant_user),
):
    if not can_manage_contacts(current_user):
        raise HTTPException(status_code=403, detail="Your account does not have the 'manage_prospects' permission.")
    try:
        mapping_in: Dict[str, str] = json.loads(mapping)
        opts: Dict = json.loads(options or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mapping and options must be JSON")
    df = _read_file(file)
    tenant_id = current_user.tenant_id

    # ── Resolve the mapping, creating new properties first (BR-CM-26) ──
    custom_defs = {d.field_key: d for d in db.query(ContactFieldDefinition).filter(
        ContactFieldDefinition.tenant_id == tenant_id)}
    required_defs = [d for d in custom_defs.values() if d.required]
    resolved: Dict[str, str] = {}
    for column, target in mapping_in.items():
        if column not in df.columns or not target or target == "skip":
            continue
        if target.startswith("new:"):
            _, ftype, label = (target.split(":", 2) + ["", ""])[:3]
            ftype = (ftype or "TEXT").upper()
            if ftype not in FIELD_TYPES or not label.strip():
                raise HTTPException(status_code=400, detail=f"Bad new property for column '{column}'")
            options_list = None
            if ftype in ("SELECT", "RADIO", "MULTI_CHECKBOX"):
                values = set()
                for v in df[column].dropna():
                    values.update(x.strip() for x in str(v).split(";") if x.strip())
                options_list = sorted(values)[:200] or ["Yes"]
            from app.routers.contacts_router import create_field_definition
            definition = create_field_definition(db, tenant_id, label.strip(), ftype, options_list)
            custom_defs[definition.field_key] = definition
            target = f"custom.{definition.field_key}"
        kind, _, field = target.partition(".")
        if (kind == "contact" and field in CONTACT_TARGETS) or (kind == "company" and field in COMPANY_TARGETS) or \
                (kind == "custom" and field in custom_defs):
            resolved[column] = target
        else:
            raise HTTPException(status_code=400, detail=f"Unknown target '{target}' for column '{column}'")
    targets = set(resolved.values())
    has_contacts = "contact.email" in targets
    has_companies = bool({"company.name", "company.domain", "company.website"} & targets)
    if not has_contacts and not has_companies:
        raise HTTPException(status_code=400, detail="Map an Email column (contacts) or a Company name/domain column (companies)")
    if not has_contacts and any(t.startswith(("contact.", "custom.")) for t in targets):
        raise HTTPException(status_code=400, detail="Contact properties need an Email column to match contacts")

    owner_id = opts.get("owner_id") or None
    if owner_id and not db.query(User.user_id).filter(User.user_id == owner_id, User.tenant_id == tenant_id).first():
        raise HTTPException(status_code=400, detail="Owner must be a user in your workspace")
    if owner_id and not can_see_owner(db, current_user, owner_id):
        raise HTTPException(status_code=403, detail="You can only assign records to yourself or your team")
    update_existing = opts.get("update_existing", True) is not False

    job = ImportJob(tenant_id=tenant_id, created_by=current_user.user_id, file_name=file.filename or "import",
                    mapping=resolved, options=opts, total_rows=len(df))
    db.add(job)
    db.commit()

    target_list = None
    if has_contacts and (opts.get("list_id") or opts.get("new_list_name")):
        if opts.get("new_list_name"):
            target_list = ProspectList(tenant_id=tenant_id, list_name=str(opts["new_list_name"]).strip()[:255],
                                       source_type="IMPORT", list_type="STATIC", uploaded_by=current_user.user_id)
            db.add(target_list)
            db.flush()
        else:
            target_list = db.query(ProspectList).filter(ProspectList.list_id == opts["list_id"],
                                                        ProspectList.tenant_id == tenant_id,
                                                        ProspectList.list_type == "STATIC").first()
            if not target_list:
                job.status, job.message = "FAILED", "Static list not found"
                db.commit()
                raise HTTPException(status_code=400, detail="Static list not found")

    owners_by_email = {u.email.lower(): u.user_id for u in db.query(User).filter(User.tenant_id == tenant_id)}
    unsubscribed = {e.lower() for (e,) in db.query(GlobalUnsubscribe.email).filter(GlobalUnsubscribe.tenant_id == tenant_id)}
    stats = {"contacts_created": 0, "contacts_updated": 0, "companies_created": 0, "companies_updated": 0}
    errors: List[dict] = []
    seen_emails: Dict[str, int] = {}
    companies_touched: set = set()
    list_members: List[str] = []

    def values_for(row, kind):
        out = {}
        for column, target in resolved.items():
            k, _, field = target.partition(".")
            if k == kind:
                value = row.get(column)
                out[field] = str(value).strip() if value is not None and str(value).strip() != "" else None
        return out

    def company_for_row(row, contact_email):
        cvals = values_for(row, "company")
        domain = crm.clean_domain(cvals.get("domain") or cvals.get("website"))
        if not domain and contact_email:
            d = crm.email_domain(contact_email)
            domain = d if d and not crm.is_personal_domain(d) else None
        name = cvals.get("name")
        if not domain and not name:
            return None
        account = None
        live = db.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.is_(None))
        if domain:
            account = live.filter(Account.domain == domain).first()
        if not account and name:
            account = live.filter(Account.name == name).first()
            if account and domain and account.domain and account.domain != domain:
                account = None
        created = False
        if not account and domain and db.query(Account.account_id).filter(
                Account.tenant_id == tenant_id, Account.domain == domain, Account.deleted_at.isnot(None)).first():
            return None  # the company is in the recycle bin: don't link to it or recreate it
        if not account:
            new_name = name or crm._name_from_domain(domain)
            if db.query(Account.account_id).filter(Account.tenant_id == tenant_id, Account.name == new_name).first():
                new_name = f"{new_name} ({domain})" if domain else f"{new_name} ({uuid.uuid4().hex[:4]})"
            account = Account(tenant_id=tenant_id, name=new_name[:255], domain=domain, lifecycle_stage="LEAD",
                              website=f"https://{domain}" if domain else None, owner_id=owner_id)
            try:
                with db.begin_nested():
                    db.add(account)
                    db.flush()
                created = True
            except IntegrityError:  # another import created it a moment ago (locking read sees it)
                account = db.query(Account).filter(
                    Account.tenant_id == tenant_id,
                    (Account.domain == domain) if domain else (Account.name == new_name)).with_for_update().first()
                if not account:
                    raise RowError("Company could not be saved (name or domain clash)")
                if account.deleted_at is not None:
                    return None
        if not created and account.owner_id and not can_see_owner(db, current_user, account.owner_id):
            return account  # link to a company outside the importer's team, but don't edit it (BR-SH-02)
        before = crm.snapshot(account, COMPANY_TARGETS)
        if domain and not account.domain:
            account.domain = domain
        for field, value in cvals.items():
            if value is None or field in ("name", "domain"):
                continue
            if field == "annual_revenue":
                try:
                    value = float(re.sub(r"[^\d.\-]", "", value))
                except ValueError:
                    raise RowError(f"Annual revenue '{value}' is not a number")
            if created or update_existing or getattr(account, field) in (None, ""):
                setattr(account, field, value)
        if created:
            stats["companies_created"] += 1
            db.add(PropertyChange(tenant_id=tenant_id, object_type="COMPANY", object_id=account.account_id,
                                  field="created", new_value=f"Imported from {job.file_name}", source="IMPORT",
                                  changed_by=current_user.user_id))
        else:
            if account.account_id not in companies_touched:
                stats["companies_updated"] += 1
            crm.record_changes(db, tenant_id, "COMPANY", account.account_id, before,
                               crm.snapshot(account, COMPANY_TARGETS), current_user.user_id, "IMPORT")
        companies_touched.add(account.account_id)
        return account

    def contact_values(row):
        vals = values_for(row, "contact")
        email = (vals.pop("email", None) or "").strip()
        if not email or not _EMAIL_RE.match(email):
            raise RowError("Missing or invalid email" if not email else f"Invalid email '{email}'")
        if vals.get("full_name") and not (vals.get("first_name") or vals.get("last_name")):
            parts = vals["full_name"].split()
            vals["first_name"], vals["last_name"] = parts[0], " ".join(parts[1:]) or None
        vals.pop("full_name", None)
        if "lifecycle_stage" in vals:
            vals["lifecycle_stage"] = _pick(vals["lifecycle_stage"], crm.LIFECYCLE_STAGES, "Lifecycle stage")
        if "lead_status" in vals:
            vals["lead_status"] = _pick(vals["lead_status"], crm.LEAD_STATUSES, "Lead status")
        if "legal_basis" in vals:
            vals["legal_basis"] = _pick(vals["legal_basis"], crm.LEGAL_BASES, "Legal basis")
        if "tags" in vals:
            vals["tags"] = normalize_tags(re.split(r"[;,]", vals["tags"] or "")) or None
        if "owner_email" in vals:
            owner_email = vals.pop("owner_email")
            if owner_email:
                if owner_email.lower() not in owners_by_email:
                    raise RowError(f"Owner '{owner_email}' is not a user in this workspace")
                if not can_see_owner(db, current_user, owners_by_email[owner_email.lower()]):
                    raise RowError(f"Owner '{owner_email}' is outside your team; you can only assign to yourself or your team")
                vals["owner_id"] = owners_by_email[owner_email.lower()]
        for k in ("phone", "mobile_phone"):
            if vals.get(k):
                vals[k] = vals[k][:50]
        custom = {}
        for column, target in resolved.items():
            if target.startswith("custom."):
                value = row.get(column)
                if value is not None and str(value).strip() != "":
                    key = target[len("custom."):]
                    custom[key] = [v.strip() for v in str(value).split(";")] \
                        if custom_defs[key].field_type == "MULTI_CHECKBOX" else str(value).strip()
        if custom:
            from app.routers.contacts_router import _clean_custom_fields
            try:
                custom = _clean_custom_fields(db, tenant_id, custom)
            except HTTPException as exc:
                raise RowError(exc.detail)
        return email, vals, custom

    def apply_contact(p, vals, custom, account, created):
        fields = tuple(f for f in crm.CONTACT_FIELDS if hasattr(Prospect, f)) + ("tags", "custom_fields")
        before = crm.snapshot(p, fields)
        for field, value in vals.items():
            if value is None:
                continue
            if field == "lifecycle_stage" and not created and not crm.lifecycle_move_allowed(p.lifecycle_stage, value, current_user):
                continue  # never move a stage backwards on import
            if created or update_existing or getattr(p, field) in (None, "", []):
                setattr(p, field, value)
        if custom:
            merged = dict(p.custom_fields or {})
            for k, v in custom.items():
                if v is not None and (created or update_existing or merged.get(k) in (None, "", [])):
                    merged[k] = v
            p.custom_fields = merged or None
        if vals.get("poc_state") and (created or update_existing):
            from app.utils.business_calendar import get_timezone_for_state
            p.timezone = get_timezone_for_state(p.poc_state)
        if account and (created or update_existing or not p.account_id):
            p.account_id = account.account_id
            p.company_name = p.company_name if (p.company_name and not created and not update_existing) else account.name
        if not created:
            crm.record_changes(db, tenant_id, "CONTACT", p.prospect_id, before, crm.snapshot(p, fields),
                               current_user.user_id, "IMPORT")

    rows = df.to_dict(orient="records")

    def process_chunk(chunk):
        """One all-or-nothing unit; the caller commits, or rolls back and retries on deadlock."""
        if not has_contacts:
            for row_no, row in chunk:
                try:
                    with db.begin_nested():
                        if not company_for_row(row, None):
                            raise RowError("Missing company name or domain")
                except RowError as exc:
                    errors.append({"row": row_no, "reason": str(exc), "values": row})
            return

        parsed = []
        for row_no, row in chunk:
            try:
                email, vals, custom = contact_values(row)
                key = email.lower()
                if key in seen_emails:
                    raise RowError(f"Duplicate of row {seen_emails[key]} in this file")
                seen_emails[key] = row_no
                parsed.append((row_no, row, email, vals, custom))
            except RowError as exc:
                errors.append({"row": row_no, "reason": str(exc), "values": row})

        existing = {p.email.lower(): p for p in db.query(Prospect).filter(
            Prospect.tenant_id == tenant_id,
            func.lower(Prospect.email).in_([e.lower() for _, _, e, _, _ in parsed]))} if parsed else {}

        for row_no, row, email, vals, custom in parsed:
            try:
                with db.begin_nested():
                    p = existing.get(email.lower())
                    if p is not None and p.deleted_at is not None:
                        raise RowError("Matches a deleted contact; restore it first")
                    if p is not None and not can_access_contact(current_user, p):
                        # Never overwrite or reassign a colleague's contact outside your team (BR-SH-02)
                        raise RowError(NOT_VISIBLE)
                    account = company_for_row(row, email)  # from columns, else the email domain (BR-CM-04)
                    if p is None:
                        p = Prospect(prospect_id=str(uuid.uuid4()), tenant_id=tenant_id, email=email,
                                     email_type="PERSONAL" if crm.is_personal_domain(crm.email_domain(email)) else "BUSINESS",
                                     email_provider=crm.email_domain(email), consent_source="IMPORT",
                                     consent_timestamp=datetime.utcnow(), is_valid_email=True,
                                     consent_status="UNSUBSCRIBED" if email.lower() in unsubscribed else "OPT_IN",
                                     owner_id=vals.get("owner_id") or owner_id or current_user.user_id,
                                     lifecycle_stage="LEAD", lead_status="NEW")
                        apply_contact(p, vals, custom, account, created=True)
                        missing = [d.label for d in required_defs if (p.custom_fields or {}).get(d.field_key) in (None, "", [])]
                        if missing:  # same rule as creating a contact in the app (BR-CM-08)
                            raise RowError(f"Missing required field: {', '.join(missing)}")
                        try:
                            with db.begin_nested():
                                db.add(p)
                                db.flush()
                            stats["contacts_created"] += 1
                            db.add(PropertyChange(tenant_id=tenant_id, object_type="CONTACT", object_id=p.prospect_id,
                                                  field="created", new_value=f"Imported from {job.file_name}",
                                                  source="IMPORT", changed_by=current_user.user_id))
                        except IntegrityError:
                            # Created by another import in the meantime: update it instead (BR-CM-28)
                            # FOR UPDATE reads the latest committed row; a plain read would use
                            # this transaction's snapshot, which predates the other import's commit
                            p = db.query(Prospect).filter(Prospect.tenant_id == tenant_id,
                                                          Prospect.email == email).with_for_update().first()
                            if p is None or p.deleted_at is not None:
                                raise RowError("Could not save contact")
                            if not can_access_contact(current_user, p):
                                raise RowError(NOT_VISIBLE)
                            apply_contact(p, vals, custom, account, created=False)
                            stats["contacts_updated"] += 1
                    else:
                        if owner_id and update_existing and "owner_id" not in vals:
                            vals["owner_id"] = owner_id
                        apply_contact(p, vals, custom, account, created=False)
                        stats["contacts_updated"] += 1
                    list_members.append(p.prospect_id)
            except RowError as exc:
                errors.append({"row": row_no, "reason": str(exc), "values": row})

    try:
        for start in range(0, len(rows), CHUNK):
            chunk = list(enumerate(rows[start:start + CHUNK], start=start + 2))  # +2: header row, 1-based
            for attempt in range(DEADLOCK_RETRIES + 1):
                saved = (dict(stats), len(errors), set(companies_touched), len(list_members), dict(seen_emails))
                try:
                    process_chunk(chunk)
                    db.commit()
                    break
                except OperationalError as exc:
                    # Simultaneous imports can deadlock on the unique keys; MySQL rolls one back.
                    # Undo this chunk's bookkeeping and run it again (BR-CM-28).
                    # 1213 deadlock / 1205 lock wait timeout; 1305 "savepoint does not exist" is what
                    # surfaces when the deadlock hit inside a savepoint (MySQL dropped the whole trx)
                    if getattr(exc.orig, "args", [None])[0] not in (1213, 1205, 1305) or attempt == DEADLOCK_RETRIES:
                        raise
                    db.rollback()
                    stats.clear()
                    stats.update(saved[0])
                    del errors[saved[1]:]
                    companies_touched.clear()
                    companies_touched.update(saved[2])
                    del list_members[saved[3]:]
                    seen_emails.clear()
                    seen_emails.update(saved[4])
                    time.sleep(random.uniform(0.1, 0.5) * (attempt + 1))

        if target_list and list_members:
            existing_members = {r[0] for r in db.query(ProspectListMember.prospect_id).filter(
                ProspectListMember.list_id == target_list.list_id)}
            for pid in dict.fromkeys(list_members):
                if pid not in existing_members:
                    db.add(ProspectListMember(list_id=target_list.list_id, prospect_id=pid, is_new_prospect=True))
        job.status = "COMPLETED"
    except Exception as exc:  # keep the history row truthful
        db.rollback()
        job = db.query(ImportJob).filter(ImportJob.import_id == job.import_id).first()
        job.status, job.message = "FAILED", f"Import stopped: {exc}"[:2000]
        raise
    finally:
        for k, v in stats.items():
            setattr(job, k, v)
        errors.sort(key=lambda e: e["row"])
        job.error_count = len(errors)
        job.errors = [{"row": e["row"], "reason": e["reason"],
                       "values": {k: (None if v is None else str(v)) for k, v in e["values"].items()}} for e in errors[:MAX_ROWS]]
        job.finished_at = datetime.utcnow()
        if target_list:
            job.options = {**(job.options or {}), "list_id": target_list.list_id, "list_name": target_list.list_name}
        db.commit()

    return {**_job_dict(job), "errors": job.errors[:50]}


@router.get("")
def import_history(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    query = db.query(ImportJob).filter(ImportJob.tenant_id == current_user.tenant_id)
    if not can_manage_contacts(current_user):
        query = query.filter(ImportJob.created_by == current_user.user_id)
    total = query.count()
    rows = query.order_by(ImportJob.started_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    names = {u.user_id: f"{u.first_name} {u.last_name}".strip()
             for u in db.query(User).filter(User.user_id.in_({r.created_by for r in rows}))}
    return {"items": [_job_dict(r, names) for r in rows], "total": total, "page": page, "page_size": page_size}


def _get_job(db: Session, user: User, import_id: str) -> ImportJob:
    job = db.query(ImportJob).filter(ImportJob.import_id == import_id, ImportJob.tenant_id == user.tenant_id).first()
    if not job or (not can_manage_contacts(user) and job.created_by != user.user_id):
        raise HTTPException(status_code=404, detail="Import not found")
    return job


@router.get("/{import_id}")
def import_detail(import_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    job = _get_job(db, current_user, import_id)
    return {**_job_dict(job), "errors": (job.errors or [])[:200]}


@router.get("/{import_id}/errors.csv")
def import_errors(import_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    """The rejected rows with the reason, ready to fix and re-import (BR-CM-29)."""
    job = _get_job(db, current_user, import_id)
    errors = job.errors or []
    columns = list(dict.fromkeys(k for e in errors for k in (e.get("values") or {})))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(crm.csv_safe_row(["Row", "Error"] + columns))
    for e in errors:
        writer.writerow(crm.csv_safe_row([e["row"], e["reason"]] + [(e.get("values") or {}).get(c) for c in columns]))
    name = re.sub(r"[^\w.-]+", "_", job.file_name.rsplit(".", 1)[0])
    return StreamingResponse(iter([out.getvalue().encode("utf-8-sig")]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}-errors.csv"'})
