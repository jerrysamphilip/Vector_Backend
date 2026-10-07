# app/db/seed_sales_demo.py
"""
Demo data for Phase 2 sales: a four-level team, leads at every stage, an SQL
queue, opportunities across this financial year's quarters (some won, some lost)
and proposals. Run the contact demo first; this works on the demo reps' contacts.

    docker compose exec api python -m app.db.seed_contacts_demo
    docker compose exec api python -m app.db.seed_sales_demo                # into the first Super Admin's workspace
    docker compose exec api python -m app.db.seed_sales_demo --reset        # remove the demo sales data only

Demo users sign in with demo-pass-123. Opportunity names start with "[Demo]".
Re-running replaces the previous demo sales data.
"""
import argparse
import random
from datetime import date, datetime, timedelta

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, Proposal
from app.models.user import User
from app.services import sales as svc

DEMO_PREFIX = "[Demo]"
DEMO_DOMAIN = "vector-demo.example"
DEMO_PASSWORD = "demo-pass-123"

# email local part -> (first, last, role, level, manager local part)
TEAM = {
    "arjun.mehta": ("Arjun", "Mehta", "MANAGER", 1, None),
    "priya.nair": ("Priya", "Nair", "MANAGER", 2, "arjun.mehta"),
    "marcus.bell": ("Marcus", "Bell", "AGENT", 3, "priya.nair"),
    "sofia.alvarez": ("Sofia", "Alvarez", "AGENT", 3, "priya.nair"),
    "leo.fischer": ("Leo", "Fischer", "AGENT", 4, "sofia.alvarez"),
}
SOURCES = ["Webinar", "Campaign reply", "Referral", "Trade show", "Website", "Partner"]
NEXT_STEPS = ["Discovery call", "Demo with the team", "Send pricing", "Security review", "Intro to CFO", "Pilot kickoff"]


def _users(db, tenant_id):
    out = {}
    for local, (first, last, role, level, _mgr) in TEAM.items():
        email = f"{local}@{DEMO_DOMAIN}"
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(tenant_id=tenant_id, first_name=first, last_name=last, email=email, role=role, status="ACTIVE",
                     password_hash=hash_password(DEMO_PASSWORD), auth_provider="local", email_verified=True)
            db.add(u)
            db.flush()
        out[local] = u
    for local, (_f, _l, _r, level, mgr) in TEAM.items():
        out[local].sales_level = level
        out[local].manager_id = out[mgr].user_id if mgr else None
    db.flush()
    return out


def reset(db, tenant_id) -> int:
    demo_ids = [u.user_id for u in db.query(User).filter(User.tenant_id == tenant_id,
                                                         User.email.like(f"%@{DEMO_DOMAIN}"))]
    opps = db.query(Opportunity).filter(Opportunity.tenant_id == tenant_id, Opportunity.name.like(f"{DEMO_PREFIX}%")).all()
    opp_ids = [o.opportunity_id for o in opps]
    if opp_ids:
        db.query(Proposal).filter(Proposal.opportunity_id.in_(opp_ids)).delete(synchronize_session=False)
        db.query(PropertyChange).filter(PropertyChange.object_type == "DEAL",
                                        PropertyChange.object_id.in_(opp_ids)).delete(synchronize_session=False)
    leads = db.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.owner_id.in_(demo_ids or ["-"])).all()
    lead_ids = [l.lead_id for l in leads]
    for o in opps:
        db.delete(o)
    db.flush()
    if lead_ids:
        # Deals made outside the demo keep existing, just without the link to a demo lead
        db.query(Opportunity).filter(Opportunity.lead_id.in_(lead_ids)).update(
            {Opportunity.lead_id: None}, synchronize_session=False)
        db.query(PropertyChange).filter(PropertyChange.object_type == "LEAD",
                                        PropertyChange.object_id.in_(lead_ids)).delete(synchronize_session=False)
        db.query(Lead).filter(Lead.lead_id.in_(lead_ids)).delete(synchronize_session=False)
    db.commit()
    return len(opps) + len(leads)


def seed(db, admin: User, rng: random.Random):
    tenant_id = admin.tenant_id
    team = _users(db, tenant_id)
    reps = [team["priya.nair"], team["marcus.bell"], team["sofia.alvarez"], team["leo.fischer"]]
    stages = svc.stages(db, tenant_id)
    open_stages = [s for s in stages if svc.stage_status(s) == "OPEN"]
    won = next(s for s in stages if s.is_won)
    lost = next(s for s in stages if s.is_lost)
    now = datetime.utcnow()
    fy_start, fy_end = svc.fiscal_year_bounds(svc.fiscal_year_of(date.today()))

    # Contacts to work: the demo reps' own, plus unowned demo-domain contacts shared out
    contacts = db.query(Prospect).filter(Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None),
                                         Prospect.email.like("%.example")).limit(400).all()
    rng.shuffle(contacts)
    open_lead_contacts = {l.prospect_id for l in db.query(Lead.prospect_id).filter(
        Lead.stage.in_(("NEW", "CONTACTED", "ENGAGED", "SQL")))}
    contacts = [c for c in contacts if c.prospect_id not in open_lead_contacts][:120]
    if len(contacts) < 20:
        raise SystemExit("Not enough demo contacts. Run `python -m app.db.seed_contacts_demo` first.")

    plan = (["NEW"] * 18 + ["CONTACTED"] * 16 + ["ENGAGED"] * 14 + ["SQL"] * 12 + ["DISQUALIFIED"] * 10 + ["CONVERTED"] * 30)
    leads = opps = proposals = 0
    for contact, stage in zip(contacts, plan):
        owner = reps[(leads + rng.randint(0, 1)) % len(reps)]
        contact.owner_id = owner.user_id
        created = now - timedelta(days=rng.randint(3, 170), hours=rng.randint(0, 23))
        lead = Lead(tenant_id=tenant_id, prospect_id=contact.prospect_id, account_id=contact.account_id,
                    owner_id=owner.user_id, source=rng.choice(SOURCES), stage="NEW", created_by=owner.user_id,
                    created_at=created, stage_changed_at=created)
        if stage in ("SQL", "CONVERTED"):
            lead.qualification = {"budget": True, "authority": True, "need": True, "timeline": True,
                                  "notes": "Qualified on discovery call"}
            lead.qualified_at = created + timedelta(days=rng.randint(4, 20))
        elif stage == "ENGAGED":
            lead.qualification = {"budget": rng.random() > 0.5, "need": True, "authority": rng.random() > 0.6}
        if stage in ("CONTACTED", "ENGAGED", "SQL"):
            lead.next_step = rng.choice(NEXT_STEPS)
            lead.next_step_at = now + timedelta(days=rng.randint(-5, 14), hours=rng.randint(9, 17))
        if stage == "DISQUALIFIED":
            lead.disqualified_reason = rng.choice(["No budget", "Not the decision maker", "Went with a competitor", "No need"])
        lead.stage = stage
        lead.stage_changed_at = (lead.qualified_at or created) + timedelta(days=rng.randint(0, 6))
        if lead.stage_changed_at > now:
            lead.stage_changed_at = now - timedelta(hours=2)
        db.add(lead)
        db.flush()
        svc.sync_contact_lifecycle(db, contact, {"NEW": "LEAD", "CONTACTED": "LEAD", "ENGAGED": "MQL", "SQL": "SQL",
                                                 "CONVERTED": "OPPORTUNITY"}.get(stage), None)
        leads += 1
        if stage != "CONVERTED":
            continue

        # Opportunity: spread over this FY's quarters; about a third closed
        roll = rng.random()
        close = fy_start + timedelta(days=rng.randint(0, (fy_end - fy_start).days - 1))
        if roll < 0.25:
            stage_row, close = won, min(close, date.today() - timedelta(days=rng.randint(1, 60)))
        elif roll < 0.35:
            stage_row, close = lost, min(close, date.today() - timedelta(days=rng.randint(1, 60)))
        else:
            stage_row = rng.choice(open_stages)
            close = max(close, date.today() + timedelta(days=rng.randint(5, 30)))
        amount = rng.choice([12000, 18000, 25000, 40000, 60000, 85000, 120000, 150000])
        company = contact.company_name or contact.full_name
        opp = Opportunity(tenant_id=tenant_id, name=f"{DEMO_PREFIX} {company} - {rng.choice(['Platform', 'Expansion', 'Pilot', 'Annual plan'])}",
                          account_id=contact.account_id, prospect_id=contact.prospect_id, lead_id=lead.lead_id,
                          owner_id=owner.user_id, stage_id=stage_row.stage_id, amount=amount, close_date=close,
                          client_type="EXISTING" if rng.random() < 0.3 else "NEW",
                          next_step=rng.choice(NEXT_STEPS) if svc.stage_status(stage_row) == "OPEN" else None,
                          created_by=owner.user_id, created_at=lead.qualified_at + timedelta(days=rng.randint(1, 10)))
        opp.status = svc.stage_status(stage_row)
        if opp.status != "OPEN":
            opp.closed_at = datetime.combine(close, datetime.min.time()) + timedelta(hours=15)
            opp.closed_reason = rng.choice(["Best fit for their workflow", "Price", "Timing", "Strong champion"])
        db.add(opp)
        db.flush()
        lead.opportunity_id, lead.converted_at = opp.opportunity_id, opp.created_at
        svc.sync_contact_lifecycle(db, contact, "CUSTOMER" if opp.status == "WON" else "OPPORTUNITY", None)
        opps += 1
        # Proposals on deals at proposal stage or later
        if stage_row.probability >= 50 or opp.status != "OPEN":
            status = ("ACCEPTED" if opp.status == "WON" else "REJECTED" if opp.status == "LOST"
                      else rng.choice(["DRAFT", "SENT", "UNDER_REVIEW"]))
            p = Proposal(tenant_id=tenant_id, opportunity_id=opp.opportunity_id, title=f"Proposal - {company}",
                         amount=amount * rng.choice([0.9, 1.0, 1.0, 1.1]), status=status, created_by=owner.user_id,
                         valid_until=date.today() + timedelta(days=30))
            if status != "DRAFT":
                p.sent_at = opp.created_at + timedelta(days=rng.randint(2, 8))
            if status in ("ACCEPTED", "REJECTED"):
                p.decided_at = opp.closed_at
            db.add(p)
            proposals += 1
    db.commit()
    return {"users": len(team), "leads": leads, "opportunities": opps, "proposals": proposals}


def main():
    parser = argparse.ArgumentParser(description="Seed demo sales data (hierarchy, leads, deals, proposals).")
    parser.add_argument("--email", help="Admin whose workspace gets the data (default: first Super Admin)")
    parser.add_argument("--reset", action="store_true", help="Only remove previously seeded demo sales data")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        query = db.query(User).filter(User.status == "ACTIVE")
        admin = (query.filter(User.email == args.email) if args.email else
                 query.filter(User.role == "SUPER_ADMIN", User.tenant_id.isnot(None)).order_by(User.created_at)).first()
        if not admin:
            raise SystemExit("No matching admin user found")
        removed = reset(db, admin.tenant_id)
        print(f"Removed {removed} previous demo sales record(s)")
        if args.reset:
            return
        result = seed(db, admin, random.Random(args.seed))
        print(f"Seeded into {admin.email}'s workspace: {result}")
        print(f"Team (password {DEMO_PASSWORD}): " + ", ".join(f"{k}@{DEMO_DOMAIN} (L{v[3]})" for k, v in TEAM.items()))
    finally:
        db.close()


if __name__ == "__main__":
    main()
