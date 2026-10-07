# app/db/seed_sales_demo.py
"""
Demo data for Phase 2 sales: a four-level team, leads at every stage, an SQL
queue, opportunities across this financial year's quarters (some won, some lost)
and proposals, plus quarterly targets, forecast categories, stale deals, email
templates and a small price list. It also fills the screens around them: demo
campaigns with sends, opens and replies (inbox conversations, reply suggestions,
campaign ROI), deal timelines (notes, calls, meetings, stage changes), tasks,
quote lines, notifications, workflow rules and saved custom reports.
Run the contact demo first; this works on the demo reps' contacts.

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
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.conversation import Conversation
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.join_tables import campaign_inboxes
from app.models.sending_inbox import SendingInbox
from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity, Proposal
from app.models.sales_extra import (MessageTemplate, Notification, Product, ProposalLine, SalesTarget, SavedReport,
                                    WorkflowRule)
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
# Quarterly targets per demo rep for the current financial year
TARGETS = {"arjun.mehta": 400000, "priya.nair": 250000, "marcus.bell": 120000, "sofia.alvarez": 120000, "leo.fischer": 60000}
PRODUCTS = [("DEMO-PLAT", "Platform licence", "seat / year", 1200), ("DEMO-ONB", "Onboarding package", "one-off", 5000),
            ("DEMO-SUP", "Premium support", "year", 8000), ("DEMO-INT", "Integration services", "day", 1500)]
TEMPLATES = [
    ("Follow-up", "Thanks for replying", "Re: next steps for {{company_name}}",
     "Hi {{first_name}},\n\nThanks for getting back to me. Would a 20-minute call this week work to walk through how "
     "{{company_name}} could use this?\n\nYou can grab a time here: {{calendar_link}}\n\nBest,\n{{your_name}}"),
    ("Meeting", "Meeting recap", "Recap: our call today",
     "Hi {{first_name}},\n\nThanks for your time today. As promised, here is a short recap and the next steps we agreed.\n\n"
     "1. \n2. \n\nBest,\n{{your_name}}"),
    ("Proposal", "Proposal sent", "Proposal for {{company_name}}",
     "Hi {{first_name}},\n\nPlease find our proposal attached. Happy to go through it whenever suits you.\n\nBest,\n{{your_name}}"),
]


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
    _reset_engagement(db, tenant_id, demo_ids, opp_ids)
    if opp_ids:
        demo_proposals = [p for (p,) in db.query(Proposal.proposal_id).filter(Proposal.opportunity_id.in_(opp_ids))]
        if demo_proposals:
            db.query(ProposalLine).filter(ProposalLine.proposal_id.in_(demo_proposals)).delete(synchronize_session=False)
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
    db.query(SalesTarget).filter(SalesTarget.tenant_id == tenant_id, SalesTarget.user_id.in_(demo_ids or ["-"])) \
        .delete(synchronize_session=False)
    db.query(MessageTemplate).filter(MessageTemplate.tenant_id == tenant_id, MessageTemplate.name.like(f"{DEMO_PREFIX}%")) \
        .delete(synchronize_session=False)
    demo_products = [p.product_id for p in db.query(Product).filter(Product.tenant_id == tenant_id, Product.sku.like("DEMO-%"))]
    if demo_products:
        db.query(ProposalLine).filter(ProposalLine.product_id.in_(demo_products)).update(
            {ProposalLine.product_id: None}, synchronize_session=False)
        db.query(Product).filter(Product.product_id.in_(demo_products)).delete(synchronize_session=False)
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
    contacts = [c for c in contacts if c.prospect_id not in open_lead_contacts]
    contacts, extra_contacts = contacts[:120], contacts[120:270]  # extras: emailed by campaigns, no lead
    if len(contacts) < 20:
        raise SystemExit("Not enough demo contacts. Run `python -m app.db.seed_contacts_demo` first.")

    plan = (["NEW"] * 18 + ["CONTACTED"] * 16 + ["ENGAGED"] * 14 + ["SQL"] * 12 + ["DISQUALIFIED"] * 10 + ["CONVERTED"] * 30)
    leads = opps = proposals = 0
    made = []  # (lead, contact, opp or None, proposal or None)
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
            made.append((lead, contact, None, None))
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
        opp.forecast_category = svc.category_for(stage_row)
        if opp.status == "OPEN" and rng.random() < 0.2:  # a rep's own call, kept when the stage moves
            opp.forecast_category, opp.forecast_category_manual = rng.choice(["COMMIT", "BEST_CASE"]), True
        if opp.status == "OPEN" and rng.random() < 0.25:  # no activity for weeks: shows as stale
            opp.updated_at = now - timedelta(days=rng.randint(20, 45))
        if opp.status != "OPEN":
            opp.closed_at = datetime.combine(close, datetime.min.time()) + timedelta(hours=15)
            opp.closed_reason = rng.choice(["Best product fit", "Price", "Strong champion"] if opp.status == "WON"
                                           else ["Price", "Lost to competitor", "Timing", "No decision"])
        db.add(opp)
        db.flush()
        made.append((lead, contact, opp, None))
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
            made[-1] = (lead, contact, opp, p)
            proposals += 1
    # Targets for this financial year, price list and templates (BR-SF-08, 10, 15)
    fy = svc.fiscal_year_of(date.today())
    for key, yearly in TARGETS.items():
        for quarter in (1, 2, 3, 4):
            db.add(SalesTarget(tenant_id=tenant_id, user_id=team[key].user_id, fy=fy, quarter=quarter,
                               amount=round(yearly / 4 * rng.choice([0.9, 1.0, 1.1]), -3), set_by=admin.user_id))
    for sku, name, unit, price in PRODUCTS:
        db.add(Product(tenant_id=tenant_id, sku=sku, name=name, unit=unit, unit_price=price, active=True))
    for category, name, subject, body in TEMPLATES:
        db.add(MessageTemplate(tenant_id=tenant_id, name=f"{DEMO_PREFIX} {name}", category=category, subject=subject,
                               body=body, shared=True, owner_id=admin.user_id, usage_count=rng.randint(0, 25)))
    db.flush()
    extra = _seed_engagement(db, admin, team, made, extra_contacts, rng, now)
    db.commit()
    return {"users": len(team), "leads": leads, "opportunities": opps, "proposals": proposals,
            "targets": len(TARGETS) * 4, "products": len(PRODUCTS), "templates": len(TEMPLATES), **extra}


# ── Engagement around the sales data (everything the other screens visualise) ──

DEMO_INBOX = f"outreach@{DEMO_DOMAIN}"
CAMPAIGNS = [("Q3 Fintech outreach", "Payments and lending teams"), ("Media & publishing", "Heads of audience and revenue"),
             ("Healthcare expansion", "Operations leaders in clinics and labs")]
POSITIVE_REPLIES = ["Yes, interested - can we talk Tuesday?", "This looks relevant. Could you send pricing?",
                    "Happy to take a call next week, I'm free Wednesday afternoon.",
                    "Forwarding to our head of ops; please include her in the follow-up.",
                    "Timing is good, we are reviewing vendors this quarter."]
NOTES = [("Discovery call", "CALL", "Walked through their current process and pain points. Budget owner is the COO."),
         ("Demo with the team", "MEETING", "Showed reporting and SSO. Strong interest from the ops lead."),
         ("Pricing questions", "EMAIL", "They asked for annual pricing and a volume discount for 80+ seats."),
         ("Internal note", "NOTE", "Champion is keen; procurement needs a security questionnaire."),
         ("Follow-up call", "CALL", "Confirmed timeline: decision expected before quarter end."),
         ("Proposal walkthrough", "MEETING", "Reviewed the proposal line by line; asked about onboarding scope."),
         ("Legal review", "NOTE", "MSA redlines sent back; two open points on liability.")]
OUTCOMES = {"CALL": ["Connected", "Left voicemail", "Connected"], "MEETING": ["Held"], "EMAIL": [None], "NOTE": [None]}
TASKS = [("Send pricing and quote", "EMAIL"), ("Book demo with the wider team", "MEETING"), ("Call to confirm budget", "CALL"),
         ("Send security questionnaire", "TODO"), ("Follow up on the proposal", "EMAIL"), ("Prepare renewal options", "TODO")]


def _reset_engagement(db, tenant_id, demo_ids, opp_ids):
    camps = [c for (c,) in db.query(Campaign.campaign_id).filter(Campaign.tenant_id == tenant_id,
                                                                  Campaign.campaign_name.like(f"{DEMO_PREFIX}%"))]
    if camps:
        msgs = [m for (m,) in db.query(EmailMessage.message_id).filter(EmailMessage.campaign_id.in_(camps))]
        convs = {c for (c,) in db.query(EmailMessage.conversation_id).filter(EmailMessage.message_id.in_(msgs or ["-"]),
                                                                              EmailMessage.conversation_id.isnot(None))}
        if msgs:
            db.query(EmailEvent).filter(EmailEvent.message_id.in_(msgs)).delete(synchronize_session=False)
            db.query(EmailMessage).filter(EmailMessage.message_id.in_(msgs)).delete(synchronize_session=False)
        if convs:
            db.query(Conversation).filter(Conversation.id.in_(convs)).delete(synchronize_session=False)
        db.query(CampaignProspect).filter(CampaignProspect.campaign_id.in_(camps)).delete(synchronize_session=False)
        db.query(EmailSequence).filter(EmailSequence.campaign_id.in_(camps)).delete(synchronize_session=False)
        db.execute(campaign_inboxes.delete().where(campaign_inboxes.c.campaign_id.in_(camps)))
        db.query(Lead).filter(Lead.campaign_id.in_(camps)).update({Lead.campaign_id: None}, synchronize_session=False)
        db.query(Opportunity).filter(Opportunity.campaign_id.in_(camps)).update({Opportunity.campaign_id: None},
                                                                                synchronize_session=False)
        db.query(Campaign).filter(Campaign.campaign_id.in_(camps)).delete(synchronize_session=False)
    db.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id, SendingInbox.email_address == DEMO_INBOX) \
        .delete(synchronize_session=False)
    task_q = db.query(CrmTask).filter(CrmTask.tenant_id == tenant_id)
    task_q.filter(CrmTask.created_by.in_(demo_ids or ["-"])).delete(synchronize_session=False)
    if opp_ids:
        db.query(CrmTask).filter(CrmTask.opportunity_id.in_(opp_ids)).delete(synchronize_session=False)
        db.query(ContactActivity).filter(ContactActivity.opportunity_id.in_(opp_ids)).delete(synchronize_session=False)
    db.query(ContactActivity).filter(ContactActivity.tenant_id == tenant_id,
                                     ContactActivity.created_by.in_(demo_ids or ["-"])).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.tenant_id == tenant_id, Notification.dedupe_key.like("demo:%")) \
        .delete(synchronize_session=False)
    db.query(WorkflowRule).filter(WorkflowRule.tenant_id == tenant_id, WorkflowRule.name.like(f"{DEMO_PREFIX}%")) \
        .delete(synchronize_session=False)
    db.query(SavedReport).filter(SavedReport.tenant_id == tenant_id, SavedReport.name.like(f"{DEMO_PREFIX}%")) \
        .delete(synchronize_session=False)
    db.flush()


def _between(rng, start, end):
    if end <= start:
        return start
    return start + timedelta(seconds=rng.randint(0, int((end - start).total_seconds())))


def _seed_engagement(db, admin, team, made, extra_contacts, rng, now):
    tenant_id = admin.tenant_id
    reps = [team["priya.nair"], team["marcus.bell"], team["sofia.alvarez"], team["leo.fischer"]]
    counts = {"campaigns": 0, "emails": 0, "replies": 0, "activities": 0, "tasks": 0, "notifications": 0}

    # Campaigns: one sender, three finished campaigns
    inbox = SendingInbox(tenant_id=tenant_id, email_address=DEMO_INBOX,
                         status="PAUSED", warmup_enabled=False, daily_limit=200)
    db.add(inbox)
    db.flush()
    campaigns = []
    for name, desc in CAMPAIGNS:
        c = Campaign(tenant_id=tenant_id, campaign_name=f"{DEMO_PREFIX} {name}", campaign_description=desc,
                     status="COMPLETED", created_by=admin.user_id, sender_name="Vector demo",
                     created_at=now - timedelta(days=rng.randint(120, 180)))
        db.add(c)
        db.flush()
        seq = EmailSequence(campaign_id=c.campaign_id, step_number=1, wait_days=0)
        db.add(seq)
        db.flush()
        db.execute(campaign_inboxes.insert().values(campaign_id=c.campaign_id, inbox_id=inbox.inbox_id))
        campaigns.append((c, seq))
        counts["campaigns"] += 1

    def email(contact, camp, seq, sent_at, reply=None, opened=True):
        out = EmailMessage(campaign_id=camp.campaign_id, prospect_id=contact.prospect_id, sequence_id=seq.sequence_id,
                           inbox_id=inbox.inbox_id, direction="OUTBOUND", status="SENT", final_status="DELIVERED",
                           subject=f"Quick idea for {contact.company_name or 'your team'}",
                           body_text=f"Hi {contact.first_name or 'there'},\n\nWe help teams like yours cut manual reporting. "
                                     "Worth a short call?\n\nBest,\nVector demo",
                           to_email=contact.email, from_email=DEMO_INBOX, scheduled_at=sent_at, sent_at=sent_at,
                           delivered_at=sent_at + timedelta(minutes=1), final_status_at=sent_at + timedelta(minutes=1))
        db.add(out)
        db.flush()
        db.add(EmailEvent(message_id=out.message_id, event_type=EmailEvent.EVENT_SENT, event_time=sent_at))
        db.add(EmailEvent(message_id=out.message_id, event_type=EmailEvent.EVENT_DELIVERED, event_time=sent_at + timedelta(minutes=1)))
        if opened or reply:
            db.add(EmailEvent(message_id=out.message_id, event_type=EmailEvent.EVENT_OPEN, event_time=sent_at + timedelta(hours=3)))
        db.add(CampaignProspect(campaign_id=camp.campaign_id, prospect_id=contact.prospect_id, current_step=1,
                                status="REPLIED" if reply else "COMPLETED", enrolled_at=sent_at - timedelta(hours=1)))
        counts["emails"] += 1
        if not reply:
            return
        reply_at = sent_at + timedelta(hours=rng.randint(5, 40))
        conv = Conversation(tenant_id=tenant_id, prospect_id=contact.prospect_id, inbox_id=inbox.inbox_id,
                            subject=out.subject, status="OPEN", is_unread=rng.random() < 0.5, last_message_at=reply_at)
        db.add(conv)
        db.flush()
        out.conversation_id = conv.id
        db.add(EmailMessage(campaign_id=camp.campaign_id, prospect_id=contact.prospect_id, conversation_id=conv.id,
                            inbox_id=inbox.inbox_id, direction="INBOUND", status="SENT", subject=f"Re: {out.subject}",
                            body_text=reply, to_email=DEMO_INBOX, from_email=contact.email, sent_at=reply_at))
        db.add(EmailEvent(message_id=out.message_id, event_type=EmailEvent.EVENT_REPLY, event_time=reply_at))
        db.add(EmailEvent(message_id=out.message_id, event_type=EmailEvent.EVENT_POSITIVE_REPLY, event_time=reply_at))
        counts["replies"] += 1

    # Most leads came from a campaign: emailed before the lead, half of them by replying
    for i, (lead, contact, opp, _p) in enumerate(made):
        if not contact.email or rng.random() < 0.2:
            continue
        camp, seq = campaigns[i % len(campaigns)]
        replied = rng.random() < 0.55
        email(contact, camp, seq, lead.created_at - timedelta(days=rng.randint(2, 6), hours=rng.randint(1, 8)),
              reply=rng.choice(POSITIVE_REPLIES) if replied else None)
        lead.campaign_id = camp.campaign_id
        if replied:
            lead.source = "Campaign reply"
        if opp:
            opp.campaign_id = camp.campaign_id
    # Contacts emailed without becoming leads (the top of the funnel); five fresh positive replies to act on
    for i, contact in enumerate(c for c in extra_contacts if c.email):
        camp, seq = campaigns[i % len(campaigns)]
        if not contact.owner_id:
            contact.owner_id = reps[i % len(reps)].user_id
        fresh = i < 5
        sent = now - (timedelta(days=rng.randint(1, 3)) if fresh else timedelta(days=rng.randint(10, 150)))
        email(contact, camp, seq, sent, reply=POSITIVE_REPLIES[i % len(POSITIVE_REPLIES)] if fresh else None,
              opened=rng.random() < 0.35)

    stage_names = {st.stage_id: st for st in svc.stages(db, tenant_id, True)}
    open_flow = [st for st in svc.stages(db, tenant_id) if svc.stage_status(st) == "OPEN"]
    products = db.query(Product).filter(Product.tenant_id == tenant_id, Product.sku.like("DEMO-%")).all()
    for lead, contact, opp, proposal in made:
        if lead.stage == "DISQUALIFIED" and rng.random() < 0.6:
            lead.recycle_at = now + timedelta(days=rng.randint(10, 80))
        if not opp:
            continue
        current = stage_names[opp.stage_id]
        end = opp.closed_at or now - timedelta(days=1)
        quiet_since = opp.updated_at if opp.updated_at and opp.updated_at < now - timedelta(days=15) else None
        if quiet_since:
            end = quiet_since
        # Stage history: up the open stages to where the deal is now (or closed)
        path = open_flow[:open_flow.index(current) + 1] if current in open_flow else \
            open_flow[:rng.randint(2, len(open_flow))] + [current]
        db.add(PropertyChange(tenant_id=tenant_id, object_type="DEAL", object_id=opp.opportunity_id, field="created",
                              new_value="Opportunity created", source="UI", changed_by=opp.owner_id, changed_at=opp.created_at))
        times = sorted(_between(rng, opp.created_at, end) for _ in range(len(path) - 1))
        for (old, new), at in zip(zip(path, path[1:]), times):
            db.add(PropertyChange(tenant_id=tenant_id, object_type="DEAL", object_id=opp.opportunity_id, field="stage_id",
                                  old_value=old.name, new_value=new.name, source="UI", changed_by=opp.owner_id, changed_at=at))
        # Activities on the deal; open deals that are not stale had one in the last week
        for _ in range(rng.randint(2, 5)):
            subject, kind, body = rng.choice(NOTES)
            db.add(ContactActivity(tenant_id=tenant_id, prospect_id=opp.prospect_id, opportunity_id=opp.opportunity_id,
                                   activity_type=kind, subject=subject, body=body, outcome=rng.choice(OUTCOMES[kind]),
                                   duration_minutes=rng.choice([15, 30, 45]) if kind in ("CALL", "MEETING") else None,
                                   occurred_at=_between(rng, opp.created_at, end), created_by=opp.owner_id,
                                   source=rng.choice(["MANUAL", "MANUAL", "GOOGLE"]) if kind in ("EMAIL", "MEETING") else "MANUAL"))
            counts["activities"] += 1
        if opp.status == "OPEN" and not quiet_since:
            db.add(ContactActivity(tenant_id=tenant_id, prospect_id=opp.prospect_id, opportunity_id=opp.opportunity_id,
                                   activity_type="CALL", subject="Check-in call", body="Agreed next steps for this week.",
                                   outcome="Connected", duration_minutes=20, created_by=opp.owner_id, source="MANUAL",
                                   occurred_at=now - timedelta(days=rng.randint(0, 6), hours=rng.randint(1, 8))))
            counts["activities"] += 1
        # Tasks: overdue, today, upcoming, done
        if opp.status == "OPEN":
            title, kind = rng.choice(TASKS)
            when = rng.choice(["overdue", "today", "today", "upcoming", "upcoming", "done"])
            due = {"overdue": now - timedelta(days=rng.randint(1, 4)), "today": now.replace(hour=15, minute=0, second=0),
                   "upcoming": now + timedelta(days=rng.randint(1, 10)), "done": now - timedelta(days=rng.randint(3, 15))}[when]
            db.add(CrmTask(tenant_id=tenant_id, prospect_id=opp.prospect_id, account_id=opp.account_id,
                           opportunity_id=opp.opportunity_id, title=title, task_type=kind,
                           priority=rng.choice(["LOW", "MEDIUM", "HIGH"]), status="DONE" if when == "done" else "OPEN",
                           due_at=due, completed_at=due if when == "done" else None, owner_id=opp.owner_id,
                           created_by=opp.owner_id))
            counts["tasks"] += 1
        # Quote lines on most proposals
        if proposal and products and rng.random() < 0.7:
            total = 0.0
            for order, prod in enumerate(rng.sample(products, rng.randint(1, min(3, len(products))))):
                qty = rng.choice([1, 2, 5, 10, 25, 50]) if prod.sku == "DEMO-PLAT" else rng.choice([1, 1, 2, 5])
                disc = rng.choice([0, 0, 5, 10])
                db.add(ProposalLine(proposal_id=proposal.proposal_id, product_id=prod.product_id, description=prod.name,
                                    quantity=qty, unit_price=prod.unit_price, discount_pct=disc, sort_order=order))
                total += float(prod.unit_price) * qty * (1 - disc / 100)
            proposal.amount = round(total, 2)

    # A few tasks for the admin running the demo, so their Home has a day
    deals = [m[2] for m in made if m[2] is not None and m[2].status == "OPEN"]
    for i, (title, kind) in enumerate(TASKS[:4]):
        opp = deals[i % len(deals)] if deals else None
        db.add(CrmTask(tenant_id=tenant_id, prospect_id=opp.prospect_id if opp else None,
                       opportunity_id=opp.opportunity_id if opp else None, title=title, task_type=kind, priority="MEDIUM",
                       status="OPEN", due_at=(now - timedelta(days=1)) if i == 0 else now.replace(hour=16, minute=30, second=0),
                       owner_id=admin.user_id, created_by=team["arjun.mehta"].user_id))
        counts["tasks"] += 1

    # Notifications (the bell): assignments, stale deals, workflow alerts
    for i, user in enumerate([admin] + reps):
        for j, (kind, title, body) in enumerate([
                ("ASSIGNED", "New lead assigned to you", "Assigned automatically from a campaign reply."),
                ("STALE_DEAL", "A deal has gone quiet", "No activity for over 14 days."),
                ("WORKFLOW", "Proposal stage reached", "A follow-up task was created by a workflow rule."),
                ("TASK", "Task due today", "Send security questionnaire")]):
            target = deals[(i + j) % len(deals)] if deals else None
            db.add(Notification(tenant_id=tenant_id, user_id=user.user_id, kind=kind, title=title, body=body,
                                link=f"/app/deals/{target.opportunity_id}" if target else None,
                                dedupe_key=f"demo:{i}:{j}", read_at=now - timedelta(hours=2) if j == 3 else None,
                                emailed_at=now, created_at=now - timedelta(hours=j * 5 + i)))
            counts["notifications"] += 1

    # Automation and saved reports
    db.add(WorkflowRule(tenant_id=tenant_id, name=f"{DEMO_PREFIX} Proposal stage: follow-up task", object_type="DEAL",
                        field="stage_id", operator="equals", value="Proposal", active=True, created_by=admin.user_id,
                        actions=[{"type": "create_task", "title": "Follow up on the proposal for {name}", "due_in_days": 3,
                                  "assign_to": "owner"}]))
    db.add(WorkflowRule(tenant_id=tenant_id, name=f"{DEMO_PREFIX} Big deal: tell the manager", object_type="DEAL",
                        field="amount", operator="greater_than", value="100000", active=True, created_by=admin.user_id,
                        actions=[{"type": "notify", "to": ["manager"], "message": "{name} is now over $100K"}]))
    for name, definition in [
            ("Pipeline value by stage", {"object": "deals", "group_by": "stage", "measure": "sum_amount", "chart": "bar",
                                         "filters": [{"field": "status", "operator": "is", "value": "OPEN"}]}),
            ("Leads by source and stage", {"object": "leads", "group_by": "source", "group_by_2": "stage", "measure": "count"}),
            ("Why we lose", {"object": "deals", "group_by": "closed_reason", "measure": "count", "chart": "bar",
                             "filters": [{"field": "status", "operator": "is", "value": "LOST"}]})]:
        db.add(SavedReport(tenant_id=tenant_id, owner_id=team["priya.nair"].user_id, name=f"{DEMO_PREFIX} {name}",
                           definition={"filters": [], "date_field": None, "date_from": None, "date_to": None,
                                       "group_by_2": None, "chart": "bar", **definition}, shared=True))
    return counts


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
