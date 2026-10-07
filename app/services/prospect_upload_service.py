# app/services/prospect_upload_service.py

import uuid
import math
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from app.models import (
    Prospect,
    ProspectList,
    ProspectListMember,
    GlobalUnsubscribe,
)
from app.utils.email_utils import parse_email
from app.utils.business_calendar import get_timezone_for_state


def _sanitize_value(val):
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

def dry_run_validate(
    df,
    db: Session,
    tenant_id: str,
    uploaded_by: str,
    file_name: str,
):
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
):
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

def confirm_upload(
    upload_id: str,
    title: str,
    records: list,
    db: Session,
    tenant_id: str,
    uploaded_by: str,
):
    """
    Master + snapshot ingestion.
    """

    safe_records = [
        r for r in records
        if isinstance(r, dict) and r.get("status") == "ACCEPTED"
    ]

    if not safe_records:
        return {
            "list_id": upload_id,
            "inserted": 0,
            "new_prospects": 0,
            "status": "NO_ACCEPTED_RECORDS",
        }

    # Collapse same-email rows within this batch (e.g. a manually edited row
    # that now collides with another accepted row's email) so we never try to
    # insert two Prospect rows with the same (tenant_id, email) in one commit.
    # Last occurrence wins, since edits are appended/overwritten in place.
    deduped_by_email = {}
    no_email_records = []
    for r in safe_records:
        email = r.get("email")
        if not email:
            no_email_records.append(r)
            continue
        deduped_by_email[email.lower()] = r
    safe_records = no_email_records + list(deduped_by_email.values())

    # Loophole #3: Re-check unsubscribe status at save time (race condition fix)
    unsubscribed = {
        e[0].lower()
        for e in db.query(GlobalUnsubscribe.email)
        .filter(GlobalUnsubscribe.tenant_id == tenant_id)
        .all()
    }

    prospect_id_map = {}
    new_prospects = set()
    skipped_unsubscribed = 0

    safe_emails = [r.get("email") for r in safe_records if r.get("email")]
    existing_prospects = (
        db.query(Prospect)
        .filter(
            Prospect.tenant_id == tenant_id,
            Prospect.email.in_(safe_emails),
        )
        .all()
    )
    existing_by_email = {
        p.email.lower(): p
        for p in existing_prospects
        if p.email
    }

    try:
        for r in safe_records:
            email = r.get("email")
            if not email:
                continue
            email_lower = email.lower()

            # Skip if email was unsubscribed after validation
            if email_lower in unsubscribed:
                skipped_unsubscribed += 1
                continue

            existing = existing_by_email.get(email_lower)

            if existing:
                # Update existing prospect with any edited fields
                existing.first_name = r.get("first_name") or existing.first_name
                existing.last_name = r.get("last_name") or existing.last_name
                existing.company_name = r.get("company_name") or existing.company_name
                existing.designation = r.get("designation") or existing.designation
                existing.industry = r.get("industry") or existing.industry
                existing.emp_band = r.get("emp_band") or existing.emp_band
                existing.linkedin_url = r.get("linkedin_url") or existing.linkedin_url
                existing.poc_state = r.get("poc_state") or existing.poc_state
                existing.poc_city = r.get("poc_city") or existing.poc_city
                existing.poc_country = r.get("poc_country") or existing.poc_country
                # Re-derive timezone from potentially updated poc_state
                if r.get("poc_state"):
                    existing.timezone = get_timezone_for_state(r.get("poc_state"))
                prospect_id_map[email] = existing.prospect_id
            else:
                prospect_id = str(uuid.uuid4())

                # Auto-derive timezone from poc_state (ensures correct timezone even after user edits)
                poc_state = r.get("poc_state")
                derived_timezone = get_timezone_for_state(poc_state) if poc_state else None

                new_prospect = Prospect(
                    prospect_id=prospect_id,
                    tenant_id=tenant_id,
                    first_name=r.get("first_name"),
                    last_name=r.get("last_name"),
                    email=email,
                    email_type=r.get("email_type"),
                    email_provider=r.get("email_provider"),
                    company_name=r.get("company_name"),
                    designation=r.get("designation"),
                    industry=r.get("industry"),
                    emp_band=r.get("emp_band"),
                    linkedin_url=r.get("linkedin_url"),
                    poc_state=poc_state,
                    poc_city=r.get("poc_city"),
                    poc_country=r.get("poc_country"),
                    timezone=derived_timezone,
                    consent_status="OPT_IN",
                    is_valid_email=True,
                )
                db.add(new_prospect)

                # Keep the existing-prospect map current so a later row in this
                # same batch that shares this email updates it instead of
                # inserting a second row with the same (tenant_id, email).
                existing_by_email[email_lower] = new_prospect

                prospect_id_map[email] = prospect_id
                new_prospects.add(email)

        db.add(
            ProspectList(
                list_id=upload_id,
                tenant_id=tenant_id,
                list_name=title,
                source_type="UPLOAD",
                uploaded_by=uploaded_by,
            )
        )

        base_added_at = datetime.utcnow()
        for idx, (email, prospect_id) in enumerate(prospect_id_map.items()):
            db.add(
                ProspectListMember(
                    id=str(uuid.uuid4()),
                    list_id=upload_id,
                    prospect_id=prospect_id,
                    is_new_prospect=email in new_prospects,
                    # Preserve original upload row order for deterministic list display.
                    # Use seconds (not microseconds) because TIMESTAMP precision may drop micros.
                    added_at=base_added_at + timedelta(seconds=idx),
                )
            )

        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Upload failed because one or more contacts could not be saved "
                "(often a duplicate email address). Please revalidate and try again."
            ),
        )
    except Exception:
        db.rollback()
        raise

    return {
        "list_id": upload_id,
        "inserted": len(prospect_id_map),
        "new_prospects": len(new_prospects),
        "skipped_unsubscribed": skipped_unsubscribed,
        "status": "CONFIRMED",
    }
