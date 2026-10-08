# app/db/seed_contacts_demo.py
"""
Demo data for contact management: reps, accounts, contacts, lists, a finished
campaign with email history, logged calls/meetings/notes, custom fields and a few
duplicates to merge.

    docker compose exec api python -m app.db.seed_contacts_demo                 # into the first Super Admin's workspace
    docker compose exec api python -m app.db.seed_contacts_demo --email you@x.com
    docker compose exec api python -m app.db.seed_contacts_demo --reset         # remove the demo data only

Re-running replaces the previous demo data. Everything it creates is recognisable and
safe: email addresses and domains use the reserved .example TLD (mail to them can't be
delivered), list and campaign names start with "[Demo]", and the campaign is COMPLETED
so the scheduler never sends anything.
"""

import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.db.seed_guard import demo_password, require_dev_environment
from app.models.account import Account
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.contact_field import ContactFieldDefinition
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.magic_login_token import MagicLoginToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.contact_service import delete_contacts
from app.utils.business_calendar import get_timezone_for_state

DEMO_PREFIX = "[Demo]"
DEMO_USER_DOMAIN = "vector-demo.example"

REPS = [
    ("Priya", "Nair", "MANAGER"),
    ("Marcus", "Bell", "AGENT"),
    ("Sofia", "Alvarez", "AGENT"),
]

ACCOUNTS = [
    # name, domain, industry, employees, city, state, country, phone, about
    ("Northwind Health", "northwind-health", "Healthcare", "1001-5000", "Boston", "MA", "USA", "+1 617 555 0142", "Regional hospital network with 14 sites."),
    ("Helix Biologics", "helixbio", "Pharma", "501-1000", "San Diego", "CA", "USA", "+1 858 555 0177", "Biologics CDMO; expanding fill-finish capacity."),
    ("Meridian Pharma", "meridianpharma", "Pharma", "5001-10000", "Mumbai", "MH", "India", "+91 22 5555 0190", "Generics manufacturer, strong in oncology."),
    ("Quantum Ledger", "quantumledger", "Fintech", "201-500", "London", None, "United Kingdom", "+44 20 5555 0123", "Payments infrastructure for mid-market banks."),
    ("Atlas Logistics", "atlaslogistics", "Logistics", "1001-5000", "Rotterdam", None, "Netherlands", "+31 10 555 0155", "Cold-chain freight across EU."),
    ("Brightline Retail", "brightline", "Retail", "5001-10000", "Chicago", "IL", "USA", "+1 312 555 0108", "Omnichannel home goods retailer."),
    ("Cobalt Robotics", "cobaltrobotics", "Manufacturing", "51-200", "Austin", "TX", "USA", "+1 512 555 0166", "Warehouse automation start-up, Series B."),
    ("Evergreen Energy", "evergreen-energy", "Energy", "1001-5000", "Denver", "CO", "USA", "+1 303 555 0131", "Utility-scale solar developer."),
    ("Lumen Diagnostics", "lumendx", "Healthcare", "201-500", "Bengaluru", "KA", "India", "+91 80 5555 0119", "Point-of-care diagnostics."),
    ("Orbital Systems", "orbitalsys", "Software", "51-200", "Berlin", None, "Germany", "+49 30 5555 0172", "Satellite data analytics platform."),
    ("Pioneer Foods", "pioneerfoods", "Food & Beverage", "1001-5000", "Toronto", "ON", "Canada", "+1 416 555 0185", "Packaged foods; modernising procurement."),
    ("Summit Insurance", "summitins", "Insurance", "5001-10000", "Hartford", "CT", "USA", "+1 860 555 0114", "P&C insurer, claims automation programme."),
    ("Vertex Semiconductors", "vertexsemi", "Manufacturing", "10000+", "Singapore", None, "Singapore", "+65 5555 0150", "Fabless chip designer."),
    ("Willow Education", "willowedu", "Education", "201-500", "Sydney", "NSW", "Australia", "+61 2 5555 0136", "Online K-12 tutoring."),
    ("Zenith Media", "zenithmedia", "Media", "501-1000", "New York", "NY", "USA", "+1 212 555 0161", "Digital publisher, 40M monthly readers."),
]

FIRST_NAMES = ["Aarav", "Olivia", "Liam", "Ananya", "Noah", "Emma", "Mateo", "Isla", "Kenji", "Amelia", "Ravi",
               "Chloe", "Lucas", "Mei", "Ethan", "Zara", "Diego", "Hannah", "Omar", "Grace", "Arjun", "Sophie",
               "Daniel", "Fatima", "Leo", "Nora", "Samuel", "Yuki", "Ibrahim", "Clara", "Vikram", "Ella",
               "Jonas", "Aisha", "Henry", "Lina", "Rohan", "Maya", "Felix", "Sara"]
LAST_NAMES = ["Sharma", "Johnson", "Okafor", "Iyer", "Müller", "Garcia", "Tanaka", "Smith", "Kowalski", "Brown",
              "Reddy", "Dubois", "Rossi", "Chen", "Williams", "Haddad", "Silva", "Nguyen", "Fischer", "Patel",
              "Lopez", "Kim", "Andersen", "Mensah", "Hughes", "Costa", "Novak", "Ali"]
TITLES = ["CEO", "CFO", "CTO", "COO", "VP Sales", "VP Operations", "Head of Procurement", "Director of IT",
          "Head of Supply Chain", "Procurement Manager", "IT Manager", "Operations Manager", "Finance Director",
          "Head of Digital", "Chief Medical Officer", "Quality Assurance Lead", "Business Analyst"]
TAGS = ["Decision maker", "Champion", "Budget holder", "Hot", "Q4", "Webinar", "Trade show", "Referral", "Nurture"]

CALL_OUTCOMES = ["Connected", "Connected", "Left voicemail", "No answer", "Busy"]
CALL_NOTES = [
    "Discussed current vendor contract; renewal is in March.",
    "Interested in a pilot for one site. Asked for pricing tiers.",
    "Gatekeeper took a message; try again Thursday morning.",
    "Wants a technical deep-dive with their IT team.",
    "Not the right person; referred us to the procurement lead.",
    "Budget approved for next quarter. Send proposal by Friday.",
    "Concerned about integration with their ERP. Share case study.",
]
MEETING_NOTES = [
    ("Discovery meeting", "Mapped their approval process: ops lead, finance and legal sign-off."),
    ("Product demo", "Demoed reporting and alerts. Strong interest in the dashboard."),
    ("Pricing review", "Walked through the proposal. They asked for a multi-year discount."),
    ("Technical workshop", "Reviewed SSO and data residency requirements with IT."),
]
NOTES = [
    "Prefers WhatsApp over email for quick questions.",
    "Met at BioAsia booth; very engaged in the panel discussion.",
    "Reports to the COO. Has influence but not final sign-off.",
    "Out of office until the 15th.",
    "Previously used a competitor; churned over support quality.",
    "Asked to be contacted only on Tuesdays and Thursdays.",
]
EMAIL_STEPS = [
    ("Quick question about {company}'s {area} plans",
     "Hi {first},\n\nI noticed {company} has been expanding its {area} team. We help similar teams cut manual work by 30%.\n\nWorth a 15-minute chat next week?"),
    ("Following up: {area} at {company}",
     "Hi {first},\n\nCircling back on my last note. Happy to share how a peer in {industry} rolled this out in six weeks."),
    ("Should I close your file?",
     "Hi {first},\n\nI haven't heard back, so I'll assume the timing isn't right. If that changes, just reply here."),
]
REPLIES = [
    "Thanks for reaching out. Can you send over a one-pager? I'll share it with the team.",
    "Interesting timing, we're reviewing this next month. Let's talk Tuesday at 10?",
    "Not a priority right now, please follow up in Q1.",
    "Please speak to our procurement lead instead; I've copied them here.",
]


def _now():
    """Naive UTC, matching how the app stores timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ago(rng, max_days, min_days=0):
    return _now() - timedelta(days=rng.uniform(min_days, max_days), minutes=rng.randint(0, 600))


def reset(db, tenant_id):
    """Remove everything this script created for the tenant."""
    demo_ids = [p.prospect_id for p in db.query(Prospect.prospect_id).filter(
        Prospect.tenant_id == tenant_id, Prospect.email.like("%.example"))]
    delete_contacts(db, demo_ids)

    db.query(GlobalUnsubscribe).filter(
        GlobalUnsubscribe.tenant_id == tenant_id, GlobalUnsubscribe.email.like("%.example")
    ).delete(synchronize_session=False)
    for model, name_col in ((Campaign, Campaign.campaign_name), (ProspectList, ProspectList.list_name)):
        rows = db.query(model).filter(model.tenant_id == tenant_id, name_col.like(f"{DEMO_PREFIX}%")).all()
        for row in rows:
            if model is ProspectList:
                db.query(ProspectListMember).filter(ProspectListMember.list_id == row.list_id).delete(synchronize_session=False)
            else:
                db.query(EmailMessage).filter(EmailMessage.campaign_id == row.campaign_id).update(
                    {EmailMessage.campaign_id: None}, synchronize_session=False)
                db.query(CampaignProspect).filter(CampaignProspect.campaign_id == row.campaign_id).delete(synchronize_session=False)
            db.delete(row)
    db.query(Account).filter(
        Account.tenant_id == tenant_id, Account.domain.like("%.example")
    ).delete(synchronize_session=False)

    demo_users = db.query(User).filter(User.tenant_id == tenant_id, User.email.like(f"%@{DEMO_USER_DOMAIN}")).all()
    removed_users = 0
    for user in demo_users:
        # Hand anything a demo rep still owns or wrote back to "nobody"
        db.query(Prospect).filter(Prospect.owner_id == user.user_id).update({Prospect.owner_id: None}, synchronize_session=False)
        db.query(Account).filter(Account.owner_id == user.user_id).update({Account.owner_id: None}, synchronize_session=False)
        db.query(ContactActivity).filter(ContactActivity.created_by == user.user_id).update(
            {ContactActivity.created_by: None}, synchronize_session=False)
        # Sign-in sessions and links from trying the demo reps
        for token_model in (RefreshToken, PasswordResetToken, MagicLoginToken):
            db.query(token_model).filter(token_model.user_id == user.user_id).delete(synchronize_session=False)
        db.flush()
        try:
            with db.begin_nested():
                db.delete(user)
            removed_users += 1
        except Exception:
            # Referenced elsewhere (e.g. they logged in): deactivate instead
            user.status = "INACTIVE"
    db.commit()
    return len(demo_ids), removed_users


def seed(db, admin: User, rng: random.Random, with_users: bool):
    require_dev_environment("seed_contacts_demo")
    tenant_id = admin.tenant_id

    # ── Reps ───────────────────────────────────────────────
    reps = [admin]
    if with_users:
        for first, last, role in REPS:
            email = f"{first.lower()}.{last.lower()}@{DEMO_USER_DOMAIN}"
            # A rep the reset couldn't delete (still referenced, e.g. by audit history) is reused
            rep = db.query(User).filter(User.tenant_id == tenant_id, User.email == email).first()
            if not rep:
                rep = User(tenant_id=tenant_id, email=email, invited_by=admin.user_id)
                db.add(rep)
            rep.first_name, rep.last_name, rep.role, rep.status = first, last, role, "ACTIVE"
            rep.password_hash, rep.email_verified = hash_password(demo_password()), True
            reps.append(rep)
        db.flush()

    # ── Custom fields (created once, reused) ───────────────
    wanted = [
        ("lead_stage", "Lead stage", "SELECT", ["Lead", "MQL", "SQL", "Opportunity", "Customer"]),
        ("deal_size_usd", "Deal size (USD)", "NUMBER", None),
        ("next_follow_up", "Next follow-up", "DATE", None),
        ("lead_source", "Lead source", "SELECT", ["Webinar", "Referral", "Trade show", "Inbound", "Outbound"]),
    ]
    existing = {f.field_key for f in db.query(ContactFieldDefinition).filter(ContactFieldDefinition.tenant_id == tenant_id)}
    for order, (key, label, ftype, options) in enumerate(wanted):
        if key not in existing:
            db.add(ContactFieldDefinition(tenant_id=tenant_id, field_key=key, label=label, field_type=ftype,
                                          options=options, sort_order=100 + order))

    # ── Accounts ───────────────────────────────────────────
    accounts = []
    for name, domain, industry, emp, city, state, country, phone, about in ACCOUNTS:
        # Never touch a real account that happens to share a demo name
        if db.query(Account.account_id).filter(Account.tenant_id == tenant_id, Account.name == name).first():
            name = f"{name} (Demo)"
        account = Account(tenant_id=tenant_id, name=name)
        db.add(account)
        account.domain = f"{domain}.example"
        account.website = f"https://www.{domain}.example"
        account.industry, account.emp_band, account.phone, account.description = industry, emp, phone, about
        account.city, account.state, account.country = city, state, country
        account.owner_id = rng.choice(reps).user_id
        accounts.append(account)
    db.flush()

    # ── Contacts ───────────────────────────────────────────
    contacts = []
    used_emails = set()
    for i in range(90):
        account = accounts[i % len(accounts)] if i < 60 else rng.choice(accounts)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        base = f"{first}.{last}".lower().replace("ü", "u")
        email = f"{base}@{account.domain}"
        while email in used_emails:
            email = f"{base}{rng.randint(2, 99)}@{account.domain}"
        used_emails.add(email)
        # Mostly the account owner, sometimes another rep, a few unassigned
        owner = account.owner_id if rng.random() < 0.7 else rng.choice(reps).user_id
        if rng.random() < 0.08:
            owner = None
        has_phone = rng.random() < 0.75
        contacts.append(Prospect(
            prospect_id=str(uuid.uuid4()), tenant_id=tenant_id,
            first_name=first, last_name=last, email=email, email_type="BUSINESS", email_provider=account.domain,
            phone=f"{account.phone[:-4]}{rng.randint(1000, 9999)}" if has_phone else None,
            mobile_phone=f"+1 555 {rng.randint(100, 999)} {rng.randint(1000, 9999)}" if rng.random() < 0.35 else None,
            designation=rng.choice(TITLES), company_name=account.name, account_id=account.account_id,
            industry=account.industry, emp_band=account.emp_band,
            linkedin_url=f"https://www.linkedin.com/in/{base.replace('.', '-')}-demo" if rng.random() < 0.6 else None,
            poc_city=account.city, poc_state=account.state, poc_country=account.country,
            timezone=get_timezone_for_state(account.state) if account.country == "USA" else None,
            owner_id=owner,
            tags=rng.sample(TAGS, rng.choice([0, 1, 1, 2, 2, 3])) or None,
            custom_fields={k: v for k, v in {
                "lead_stage": rng.choice(["Lead", "Lead", "MQL", "SQL", "Opportunity", "Customer"]),
                "deal_size_usd": rng.choice([None, 12000, 25000, 48000, 90000, 150000]),
                "next_follow_up": (_now() + timedelta(days=rng.randint(1, 30))).date().isoformat() if rng.random() < 0.4 else None,
                "lead_source": rng.choice(["Webinar", "Referral", "Trade show", "Inbound", "Outbound"]),
            }.items() if v is not None},
            consent_status="OPT_IN", consent_source="DEMO", consent_timestamp=_ago(rng, 120, 60),
            is_valid_email=True, created_at=_ago(rng, 120, 1),
        ))

    # Deliberate duplicates for "Find duplicates": same person, second email / same phone
    for original, alt_domain in ((contacts[0], "gmail.example"), (contacts[7], "outlook.example"), (contacts[15], None)):
        dup = Prospect(
            prospect_id=str(uuid.uuid4()), tenant_id=tenant_id,
            first_name=original.first_name, last_name=original.last_name,
            email=(f"{original.first_name}.{original.last_name}.home@{alt_domain}".lower() if alt_domain
                   else f"{original.first_name[0]}{original.last_name}@{original.email.split('@')[1]}".lower()),
            email_type="PERSONAL" if alt_domain else "BUSINESS",
            phone=original.phone or "+1 617 555 7777", designation=original.designation,
            company_name=original.company_name, account_id=original.account_id,
            owner_id=rng.choice(reps).user_id, tags=["Trade show"],
            consent_status="OPT_IN", consent_source="DEMO", is_valid_email=True, created_at=_ago(rng, 30),
        )
        if not original.phone:
            original.phone = dup.phone
        contacts.append(dup)
    db.add_all(contacts)
    db.flush()

    # ── Lists ──────────────────────────────────────────────
    for list_name, members in ((f"{DEMO_PREFIX} BioAsia trade show", contacts[:30]),
                               (f"{DEMO_PREFIX} September webinar", contacts[25:55])):
        plist = ProspectList(list_id=str(uuid.uuid4()), tenant_id=tenant_id, list_name=list_name,
                             source_type="UPLOAD", uploaded_by=admin.user_id, uploaded_at=_ago(rng, 90, 60))
        db.add(plist)
        db.flush()
        for j, contact in enumerate(members):
            db.add(ProspectListMember(list_id=plist.list_id, prospect_id=contact.prospect_id,
                                      added_at=plist.uploaded_at + timedelta(seconds=j),
                                      notes=rng.choice(NOTES) if rng.random() < 0.1 else None))

    # ── Finished campaign with email history ───────────────
    campaign = Campaign(tenant_id=tenant_id, campaign_name=f"{DEMO_PREFIX} Q3 operations outreach",
                        campaign_description="Demo campaign (completed). Created by seed_contacts_demo.",
                        status="COMPLETED", created_by=admin.user_id, sender_name=f"{admin.first_name} {admin.last_name}",
                        created_at=_ago(rng, 70, 65))
    db.add(campaign)
    db.flush()
    sender = admin.email
    for contact in contacts[:40]:
        account = next(a for a in accounts if a.account_id == contact.account_id)
        steps = rng.choice([1, 2, 2, 3, 3])
        first_send = _ago(rng, 60, 30)
        status, replied = "COMPLETED", False
        for step in range(steps):
            subject, body = EMAIL_STEPS[step]
            ctx = {"first": contact.first_name, "company": account.name, "area": "operations", "industry": account.industry.lower()}
            sent_at = first_send + timedelta(days=4 * step, minutes=rng.randint(0, 90))
            bounced = step == 0 and rng.random() < 0.05
            msg = EmailMessage(campaign_id=campaign.campaign_id, prospect_id=contact.prospect_id, direction="OUTBOUND",
                               status="BOUNCED" if bounced else "SENT", subject=subject.format(**ctx),
                               body_text=body.format(**ctx), to_email=contact.email, from_email=sender,
                               scheduled_at=sent_at, sent_at=sent_at, delivered_at=None if bounced else sent_at)
            db.add(msg)
            db.flush()
            if bounced:
                db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_BOUNCE, event_time=sent_at + timedelta(minutes=2)))
                contact.is_valid_email, status = False, "BOUNCED"
                break
            if rng.random() < 0.6:
                opened = sent_at + timedelta(hours=rng.uniform(0.2, 30))
                db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_OPEN, event_time=opened))
                if rng.random() < 0.25:
                    db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_CLICK, event_time=opened + timedelta(minutes=1)))
                if rng.random() < 0.18:
                    reply_at = opened + timedelta(hours=rng.uniform(1, 20))
                    db.add(EmailMessage(campaign_id=campaign.campaign_id, prospect_id=contact.prospect_id, direction="INBOUND",
                                        status="DELIVERED", subject=f"Re: {msg.subject}", body_text=rng.choice(REPLIES),
                                        to_email=sender, from_email=contact.email, sent_at=reply_at, delivered_at=reply_at))
                    db.add(EmailEvent(message_id=msg.message_id, event_type=EmailEvent.EVENT_REPLY, event_time=reply_at))
                    status, replied = "REPLIED", True
                    break
        if not replied and status != "BOUNCED" and rng.random() < 0.04:
            status = "UNSUBSCRIBED"
            contact.consent_status = "UNSUBSCRIBED"
            db.add(GlobalUnsubscribe(tenant_id=tenant_id, email=contact.email, reason="Demo unsubscribe"))
        db.add(CampaignProspect(campaign_id=campaign.campaign_id, prospect_id=contact.prospect_id,
                                current_step=min(steps, 3), status=status, enrolled_at=first_send - timedelta(hours=1),
                                stopped_reason=status.lower() if status != "COMPLETED" else None))

    # ── Logged activities ──────────────────────────────────
    activity_count = 0
    for contact in rng.sample(contacts, 55):
        author = contact.owner_id or admin.user_id
        for _ in range(rng.randint(1, 4)):
            kind = rng.choice(["CALL", "CALL", "NOTE", "MEETING"])
            when = _ago(rng, 45)
            if kind == "CALL":
                outcome = rng.choice(CALL_OUTCOMES)
                activity = ContactActivity(activity_type="CALL", subject=rng.choice(["Intro call", "Follow-up call", "Check-in call"]),
                                           outcome=outcome, duration_minutes=rng.randint(3, 35) if outcome == "Connected" else None,
                                           body=rng.choice(CALL_NOTES) if outcome == "Connected" else None)
            elif kind == "MEETING":
                subject, body = rng.choice(MEETING_NOTES)
                activity = ContactActivity(activity_type="MEETING", subject=subject, body=body, duration_minutes=rng.choice([30, 45, 60]))
            else:
                activity = ContactActivity(activity_type="NOTE", body=rng.choice(NOTES))
            activity.tenant_id, activity.prospect_id, activity.created_by = tenant_id, contact.prospect_id, author
            activity.occurred_at = activity.created_at = when
            db.add(activity)
            activity_count += 1

    db.commit()
    return {"reps": len(reps) - 1, "accounts": len(accounts), "contacts": len(contacts),
            "activities": activity_count, "campaign_contacts": 40}


def main():
    require_dev_environment("seed_contacts_demo")
    parser = argparse.ArgumentParser(description="Seed demo contacts, accounts and activity.")
    parser.add_argument("--email", help="Admin whose workspace gets the data (default: first Super Admin)")
    parser.add_argument("--reset", action="store_true", help="Only remove previously seeded demo data")
    parser.add_argument("--no-users", action="store_true", help="Don't create the three demo reps")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (same seed, same data)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = db.query(User).filter(User.status == "ACTIVE")
        admin = (query.filter(User.email == args.email) if args.email
                 else query.filter(User.role == "SUPER_ADMIN").order_by(User.created_at)).first()
        if not admin or not admin.tenant_id:
            raise SystemExit("No matching workspace admin found. Register an account first, or pass --email.")

        removed, removed_users = reset(db, admin.tenant_id)
        if removed or removed_users:
            print(f"Removed previous demo data: {removed} contacts, {removed_users} demo users.")
        if args.reset:
            return

        counts = seed(db, admin, random.Random(args.seed), with_users=not args.no_users)
        print(f"Seeded workspace of {admin.email}: {counts['contacts']} contacts, {counts['accounts']} accounts, "
              f"{counts['activities']} activities, {counts['campaign_contacts']} contacts with campaign email history, "
              f"{counts['reps']} demo reps.")
        if counts["reps"]:
            print(f"Demo reps sign in as <first>.<last>@{DEMO_USER_DOMAIN} with password '{demo_password()}' "
                  f"(e.g. marcus.bell@{DEMO_USER_DOMAIN}, an Agent who only sees their own contacts).")
        print("Remove it any time with: python -m app.db.seed_contacts_demo --reset")
    finally:
        db.close()


if __name__ == "__main__":
    main()
