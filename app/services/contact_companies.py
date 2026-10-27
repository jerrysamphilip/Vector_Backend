"""
A contact's companies (CM-014): one primary company (prospects.account_id, mirrored by the
link row with is_primary) and any number of secondary associations, each with an optional
label such as "Decision maker" or "Billing".

Code that sets prospects.account_id directly (imports, merge, bulk edit) never has to touch
the link table: queries only read secondary rows from it, and sync_primary() brings the rows
back in line with account_id whenever a contact's companies are read or edited.

The rules live here; the SQL in app/repositories/contacts.py. Functions taking ``db`` keep their
signatures for callers outside the contacts aggregate.
"""
from typing import Any, Iterable, List, Optional, Union

from app.models.crm import ContactCompany
from app.models.prospect import Prospect
from app.repositories.contacts import ContactRepository, Session, SqlAlchemyContactRepository
from app.repositories.contacts import company_condition as _company_condition
from app.repositories.contacts import secondary_contact_ids as _secondary_contact_ids

RepoOrSession = Union[ContactRepository, Session]


def _repo(source: RepoOrSession) -> ContactRepository:
    """Callers outside the aggregate still pass a session; services pass their repository."""
    return SqlAlchemyContactRepository(source) if isinstance(source, Session) else source


def secondary_contact_ids(db: Session, account_id: str) -> Any:
    """Subquery: contacts associated with the company other than as their primary company."""
    return _secondary_contact_ids(db, account_id)


def company_condition(db: Session, account_id: str) -> Any:
    """Contacts whose primary company is account_id, or that are associated with it."""
    return _company_condition(db, account_id)


def sync_primary(db: RepoOrSession, prospect: Prospect, user_id: Optional[str] = None) -> None:
    """Make the link rows agree with prospects.account_id. A primary row whose company was
    replaced is kept as a secondary association when it has a label, otherwise removed."""
    repo = _repo(db)
    current = None
    for row in repo.links_of(prospect.prospect_id):
        if row.account_id == prospect.account_id:
            current = row
            if not row.is_primary:
                row.is_primary = True
        elif row.is_primary:
            if row.label:
                row.is_primary = False
            else:
                repo.remove(row)
    if prospect.account_id and current is None:
        repo.stage(ContactCompany(tenant_id=prospect.tenant_id, prospect_id=prospect.prospect_id,
                                  account_id=prospect.account_id, is_primary=True, created_by=user_id))
    repo.flush()


def _link_dict(link: Any, a: Any, primary: bool) -> dict:
    return {"link_id": link.link_id if link else None, "account_id": a.account_id, "name": a.name,
            "domain": a.domain, "industry": a.industry, "label": link.label if link else None,
            "is_primary": primary, "created_at": link.created_at if link else None}


def links(db: RepoOrSession, prospect: Prospect) -> List[dict]:
    """The contact's live (not deleted) companies, primary first. Read-only: the primary is
    whatever prospects.account_id says, even before sync_primary() has run."""
    repo = _repo(db)
    rows = {link.account_id: (link, a) for link, a in repo.links_with_accounts(prospect)}
    out = []
    for account_id, (link, a) in rows.items():
        primary = account_id == prospect.account_id
        if link.is_primary and not primary and not link.label:
            continue  # the mirror of a replaced primary company; sync_primary() removes it
        out.append(_link_dict(link, a, primary))
    if prospect.account_id and prospect.account_id not in rows:
        a = repo.live_account(prospect.tenant_id, prospect.account_id)
        if a:
            out.append(_link_dict(None, a, True))
    out.sort(key=lambda r: (not r["is_primary"], (r["name"] or "").lower()))
    return out


def labels_for_company(db: RepoOrSession, account_id: str, prospect_ids: Iterable[str]) -> dict:
    """{prospect_id: association label} for the company's contacts (no entry = no label)."""
    ids = list(prospect_ids)
    if not ids:
        return {}
    return _repo(db).labels_for_company(account_id, ids)


def move_links(db: RepoOrSession, source: Prospect, target: Prospect) -> None:
    """Merge: the duplicate's associations move to the surviving contact as secondary ones."""
    repo = _repo(db)
    have = set(repo.linked_account_ids(target.prospect_id))
    for row in repo.links_of(source.prospect_id):
        if row.account_id in have or row.account_id == target.account_id:
            repo.remove(row)
        else:
            row.prospect_id, row.is_primary = target.prospect_id, False
            have.add(row.account_id)
    repo.flush()
