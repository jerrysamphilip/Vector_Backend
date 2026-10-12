"""
A contact's companies (CM-014): one primary company (prospects.account_id, mirrored by the
link row with is_primary) and any number of secondary associations, each with an optional
label such as "Decision maker" or "Billing".

Code that sets prospects.account_id directly (imports, merge, bulk edit) never has to touch
the link table: queries only read secondary rows from it, and sync_primary() brings the rows
back in line with account_id whenever a contact's companies are read or edited.
"""
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.crm import ContactCompany
from app.models.prospect import Prospect


def secondary_contact_ids(db: Session, account_id: str):
    """Subquery: contacts associated with the company other than as their primary company."""
    return db.query(ContactCompany.prospect_id).filter(
        ContactCompany.account_id == account_id, ContactCompany.is_primary.is_(False))


def company_condition(db: Session, account_id: str):
    """Contacts whose primary company is account_id, or that are associated with it."""
    return or_(Prospect.account_id == account_id, Prospect.prospect_id.in_(secondary_contact_ids(db, account_id)))


def sync_primary(db: Session, prospect: Prospect, user_id: Optional[str] = None) -> None:
    """Make the link rows agree with prospects.account_id. A primary row whose company was
    replaced is kept as a secondary association when it has a label, otherwise removed."""
    rows = db.query(ContactCompany).filter(ContactCompany.prospect_id == prospect.prospect_id).all()
    current = None
    for row in rows:
        if row.account_id == prospect.account_id:
            current = row
            if not row.is_primary:
                row.is_primary = True
        elif row.is_primary:
            if row.label:
                row.is_primary = False
            else:
                db.delete(row)
    if prospect.account_id and current is None:
        db.add(ContactCompany(tenant_id=prospect.tenant_id, prospect_id=prospect.prospect_id,
                              account_id=prospect.account_id, is_primary=True, created_by=user_id))
    db.flush()


def links(db: Session, prospect: Prospect) -> List[dict]:
    """The contact's live (not deleted) companies, primary first. Read-only: the primary is
    whatever prospects.account_id says, even before sync_primary() has run."""
    rows = {link.account_id: (link, a) for link, a in db.query(ContactCompany, Account).join(
        Account, Account.account_id == ContactCompany.account_id).filter(
        ContactCompany.prospect_id == prospect.prospect_id, Account.tenant_id == prospect.tenant_id,
        Account.deleted_at.is_(None))}
    out = []
    for account_id, (link, a) in rows.items():
        primary = account_id == prospect.account_id
        if link.is_primary and not primary and not link.label:
            continue  # the mirror of a replaced primary company; sync_primary() removes it
        out.append({"link_id": link.link_id, "account_id": a.account_id, "name": a.name, "domain": a.domain,
                    "industry": a.industry, "label": link.label, "is_primary": primary,
                    "created_at": link.created_at})
    if prospect.account_id and prospect.account_id not in rows:
        a = db.query(Account).filter(Account.account_id == prospect.account_id, Account.tenant_id == prospect.tenant_id,
                                     Account.deleted_at.is_(None)).first()
        if a:
            out.append({"link_id": None, "account_id": a.account_id, "name": a.name, "domain": a.domain,
                        "industry": a.industry, "label": None, "is_primary": True, "created_at": None})
    out.sort(key=lambda r: (not r["is_primary"], (r["name"] or "").lower()))
    return out


def labels_for_company(db: Session, account_id: str, prospect_ids) -> dict:
    """{prospect_id: association label} for the company's contacts (no entry = no label)."""
    ids = list(prospect_ids)
    if not ids:
        return {}
    return {pid: label for pid, label in db.query(ContactCompany.prospect_id, ContactCompany.label).filter(
        ContactCompany.account_id == account_id, ContactCompany.prospect_id.in_(ids), ContactCompany.label.isnot(None))}


def move_links(db: Session, source: Prospect, target: Prospect) -> None:
    """Merge: the duplicate's associations move to the surviving contact as secondary ones."""
    have = {r.account_id for r in db.query(ContactCompany.account_id).filter(
        ContactCompany.prospect_id == target.prospect_id)}
    for row in db.query(ContactCompany).filter(ContactCompany.prospect_id == source.prospect_id).all():
        if row.account_id in have or row.account_id == target.account_id:
            db.delete(row)
        else:
            row.prospect_id, row.is_primary = target.prospect_id, False
            have.add(row.account_id)
    db.flush()
