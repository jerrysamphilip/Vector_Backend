# app/db/seed_uat_users.py
"""
Create a UAT workspace with placeholder test users, each with a random password printed once.

    kubectl exec -n <namespace> deploy/backend -- python -m app.db.seed_uat_users
    kubectl exec -n <namespace> deploy/backend -- python -m app.db.seed_uat_users --domain uat.acme.com --per-role 2

Users (with --per-role 2): uat.superadmin1/2, uat.admin1/2, uat.manager1/2 and uat.agent1/2 at
--domain. uat.superadmin1 owns the workspace; agent N reports to manager N. Signing in needs only
the email and password: no invitation email is sent.

Unlike the demo seeds this runs outside development, because UAT needs it, but never in
production (ENVIRONMENT=production). Passwords are random (secrets.token_urlsafe), shown once on
stdout and never logged or stored in clear. Running it again creates only the missing users and
leaves existing accounts and their passwords alone.
"""
import argparse
import secrets
import sys
from dataclasses import dataclass
from typing import List, Optional

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Tenant, User

ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
ROLE_SLUG = {"SUPER_ADMIN": "superadmin", "ADMIN": "admin", "MANAGER": "manager", "AGENT": "agent"}
ROLE_NAME = {"SUPER_ADMIN": "Super Admin", "ADMIN": "Admin", "MANAGER": "Manager", "AGENT": "Agent"}
LIVE_STATUSES = ("ACTIVE", "INVITED")


@dataclass
class Planned:
    email: str
    role: str
    number: int
    first_name: str
    last_name: str


@dataclass
class Outcome:
    email: str
    role: str
    password: Optional[str]  # None: the account already existed and was left alone
    note: str = ""


def plan(domain: str, per_role: int) -> List[Planned]:
    """The placeholder users, in role order (the first Super Admin owns the workspace)."""
    users = []
    for role in ROLES:
        for n in range(1, per_role + 1):
            users.append(Planned(email=f"uat.{ROLE_SLUG[role]}{n}@{domain}".lower(), role=role, number=n,
                                 first_name="UAT", last_name=f"{ROLE_NAME[role]} {n}"))
    return users


def new_password() -> str:
    return secrets.token_urlsafe(12)  # 16 characters, about 96 bits


def refuse_production() -> None:
    if settings.is_production:
        raise SystemExit("seed_uat_users does not run when ENVIRONMENT is production. Refusing to seed.")


def _live_account(db, email: str) -> Optional[User]:
    """The ACTIVE or INVITED account with this email in any workspace (one per email, F017)."""
    return db.query(User).filter(User.email == email, User.status.in_(LIVE_STATUSES)).first()


def seed(db, domain: str, per_role: int, workspace: str) -> List[Outcome]:
    refuse_production()
    planned = plan(domain, per_role)
    owner_plan = planned[0]
    outcomes: List[Outcome] = []

    owner = _live_account(db, owner_plan.email)
    if owner is None:
        tenant = Tenant(tenant_name=workspace)
        db.add(tenant)
        db.flush()
        password = new_password()
        owner = User(tenant_id=tenant.tenant_id, first_name=owner_plan.first_name, last_name=owner_plan.last_name,
                     email=owner_plan.email, password_hash=hash_password(password), role="SUPER_ADMIN",
                     status="ACTIVE", auth_provider="local", email_verified=True)
        db.add(owner)
        db.flush()
        outcomes.append(Outcome(owner.email, owner.role, password, f"owner of new workspace '{workspace}'"))
    else:
        outcomes.append(Outcome(owner.email, owner.role, None, "already exists, unchanged"))
    tenant_id = owner.tenant_id

    managers = {}
    for item in planned[1:]:
        user = _live_account(db, item.email)
        if user is not None:
            note = "already exists, unchanged" if user.tenant_id == tenant_id else "exists in another workspace, skipped"
            outcomes.append(Outcome(item.email, user.role, None, note))
        else:
            password = new_password()
            user = User(tenant_id=tenant_id, first_name=item.first_name, last_name=item.last_name, email=item.email,
                        password_hash=hash_password(password), role=item.role, status="ACTIVE",
                        auth_provider="local", email_verified=True, invited_by=owner.user_id)
            if item.role == "AGENT" and item.number in managers:
                user.manager_id = managers[item.number]
            db.add(user)
            db.flush()
            note = ""
            if user.manager_id:
                note = f"reports to uat.manager{item.number}@{domain}"
            outcomes.append(Outcome(item.email, item.role, password, note))
        if item.role == "MANAGER" and user.tenant_id == tenant_id:
            managers[item.number] = user.user_id
    db.commit()
    return outcomes


def print_outcomes(outcomes: List[Outcome], out=sys.stdout) -> None:
    print("\nUAT users (passwords are shown only now; hand them out over a secure channel):\n", file=out)
    width = max(len(o.email) for o in outcomes)
    print(f"{'EMAIL'.ljust(width)}  {'ROLE'.ljust(11)}  {'PASSWORD'.ljust(16)}  NOTE", file=out)
    for o in outcomes:
        print(f"{o.email.ljust(width)}  {o.role.ljust(11)}  {(o.password or '-').ljust(16)}  {o.note}", file=out)
    created = sum(1 for o in outcomes if o.password)
    print(f"\n{created} created, {len(outcomes) - created} left unchanged.", file=out)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Create a UAT workspace with placeholder test users.")
    parser.add_argument("--domain", default="outreach360-uat.example",
                        help="Email domain of the placeholder users (default: outreach360-uat.example)")
    parser.add_argument("--per-role", type=int, default=2, help="Users per role (default: 2)")
    parser.add_argument("--workspace", default="UAT Workspace", help="Name of the new workspace")
    args = parser.parse_args(argv)
    if not 1 <= args.per_role <= 20:
        parser.error("--per-role must be between 1 and 20")
    refuse_production()
    db = SessionLocal()
    try:
        print_outcomes(seed(db, args.domain.strip().lower(), args.per_role, args.workspace.strip() or "UAT Workspace"))
    finally:
        db.close()


if __name__ == "__main__":
    main()
