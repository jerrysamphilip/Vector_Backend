# app/db/seed_data.py
"""
Seed database with dummy data for Campaign Manager.
"""

from datetime import datetime, date, time, timedelta
import uuid

from sqlalchemy.orm import Session
from app.core.database import get_db_session
from app.models.tenant import Tenant
from app.models.user import User
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.campaign import Campaign, CampaignStateEvent, CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.email_template import AIPrompt, EmailTemplate
from app.models.metrics import CampaignMetricsRealtime
from app.models.audit import AuditLog


def seed_database():
    """Seed database with dummy data."""
    
    with get_db_session() as db:
        print("Seeding database with dummy data...")
        
        # =====================
        # 1. Create Tenant
        # =====================
        tenant_id = "00000000-0000-0000-0000-000000000001"
        tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
        
        if not tenant:
            tenant = Tenant(
                tenant_id=tenant_id,
                tenant_name="Neutrino Tech Systems",
                status="ACTIVE",
            )
            db.add(tenant)
            print("[OK] Created tenant: Neutrino Tech Systems")
        
        # =====================
        # 2. Create Users
        # =====================
        users_data = [
            {"user_id": "00000000-0000-0000-0000-000000000001", "first_name": "John", "last_name": "Doe", "email": "john.doe@neutrinotech.com", "role": "ADMIN"},
            {"user_id": "00000000-0000-0000-0000-000000000002", "first_name": "Jane", "last_name": "Smith", "email": "jane.smith@neutrinotech.com", "role": "MANAGER"},
            {"user_id": "00000000-0000-0000-0000-000000000003", "first_name": "Bob", "last_name": "Wilson", "email": "bob.wilson@neutrinotech.com", "role": "AGENT"},
        ]
        
        for user_data in users_data:
            existing = db.query(User).filter(User.user_id == user_data["user_id"]).first()
            if not existing:
                user = User(
                    tenant_id=tenant_id,
                    **user_data,
                    status="ACTIVE",
                )
                db.add(user)
                print(f"[OK] Created user: {user_data['first_name']} {user_data['last_name']}")
        
        db.flush()  # Flush users before creating prospects
        
        # =====================
        # 3. Create Prospects
        # =====================
        prospects_data = [
            {"prospect_id": "p0000000-0000-0000-0000-000000000001", "first_name": "Michael", "last_name": "Chen", "email": "michael.chen@techcorp.io", "company_name": "TechCorp Solutions", "designation": "VP of Engineering", "industry": "Technology"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000002", "first_name": "Sarah", "last_name": "Johnson", "email": "sarah.j@dataflow.com", "company_name": "DataFlow Inc", "designation": "CTO", "industry": "Data Analytics"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000003", "first_name": "David", "last_name": "Martinez", "email": "david.m@cloudsync.io", "company_name": "CloudSync", "designation": "Director of IT", "industry": "Cloud Infrastructure"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000004", "first_name": "Emily", "last_name": "Williams", "email": "emily.w@innovatehub.com", "company_name": "InnovateHub", "designation": "Head of Product", "industry": "SaaS"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000005", "first_name": "James", "last_name": "Brown", "email": "jbrown@scalefast.io", "company_name": "ScaleFast", "designation": "CEO", "industry": "E-commerce"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000006", "first_name": "Lisa", "last_name": "Anderson", "email": "lisa.a@fintech360.com", "company_name": "FinTech360", "designation": "VP of Operations", "industry": "Financial Technology"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000007", "first_name": "Robert", "last_name": "Taylor", "email": "rtaylor@aiventures.co", "company_name": "AI Ventures", "designation": "Head of AI", "industry": "Artificial Intelligence"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000008", "first_name": "Jennifer", "last_name": "Garcia", "email": "jen.garcia@healthlogic.io", "company_name": "HealthLogic", "designation": "Chief Digital Officer", "industry": "Healthcare Tech"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000009", "first_name": "Christopher", "last_name": "Lee", "email": "c.lee@greentech.com", "company_name": "GreenTech Solutions", "designation": "VP of Sales", "industry": "Clean Technology"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000010", "first_name": "Amanda", "last_name": "White", "email": "amanda.w@nexusai.io", "company_name": "Nexus AI", "designation": "Director of Engineering", "industry": "Machine Learning"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000011", "first_name": "Daniel", "last_name": "Harris", "email": "dharris@marketpro.com", "company_name": "MarketPro", "designation": "CMO", "industry": "Marketing Technology"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000012", "first_name": "Jessica", "last_name": "Clark", "email": "jclark@retailedge.io", "company_name": "RetailEdge", "designation": "Head of Digital", "industry": "Retail Tech"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000013", "first_name": "Matthew", "last_name": "Lewis", "email": "matt.lewis@cybershield.com", "company_name": "CyberShield", "designation": "CISO", "industry": "Cybersecurity"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000014", "first_name": "Rachel", "last_name": "Walker", "email": "rwalker@logisticspro.io", "company_name": "LogisticsPro", "designation": "VP of Technology", "industry": "Supply Chain"},
            {"prospect_id": "p0000000-0000-0000-0000-000000000015", "first_name": "Andrew", "last_name": "Hall", "email": "ahall@edtechplus.com", "company_name": "EdTech Plus", "designation": "Product Director", "industry": "Education Technology"},
        ]
        
        for prospect_data in prospects_data:
            existing = db.query(Prospect).filter(Prospect.prospect_id == prospect_data["prospect_id"]).first()
            if not existing:
                prospect = Prospect(
                    tenant_id=tenant_id,
                    is_valid_email=True,
                    consent_status="OPT_IN",
                    **prospect_data,
                )
                db.add(prospect)
        print(f"[OK] Created {len(prospects_data)} prospects")
        
        db.flush()
        
        # =====================
        # 4. Create Prospect Lists
        # =====================
        lists_data = [
            {"list_id": "l0000000-0000-0000-0000-000000000001", "list_name": "Tech Startups Q1 2025", "source_type": "CSV", "uploaded_by": "00000000-0000-0000-0000-000000000001"},
            {"list_id": "l0000000-0000-0000-0000-000000000002", "list_name": "Enterprise Decision Makers", "source_type": "CRM", "uploaded_by": "00000000-0000-0000-0000-000000000002"},
            {"list_id": "l0000000-0000-0000-0000-000000000003", "list_name": "SMB Outreach List", "source_type": "CSV", "uploaded_by": "00000000-0000-0000-0000-000000000001"},
        ]
        
        for list_data in lists_data:
            existing = db.query(ProspectList).filter(ProspectList.list_id == list_data["list_id"]).first()
            if not existing:
                pl = ProspectList(tenant_id=tenant_id, **list_data)
                db.add(pl)
        print(f"[OK] Created {len(lists_data)} prospect lists")
        
        db.flush()
        
        # =====================
        # 5. Assign Prospects to Lists
        # =====================
        list_members = [
            # Tech Startups list - 5 prospects
            {"list_id": "l0000000-0000-0000-0000-000000000001", "prospect_id": "p0000000-0000-0000-0000-000000000001"},
            {"list_id": "l0000000-0000-0000-0000-000000000001", "prospect_id": "p0000000-0000-0000-0000-000000000002"},
            {"list_id": "l0000000-0000-0000-0000-000000000001", "prospect_id": "p0000000-0000-0000-0000-000000000003"},
            {"list_id": "l0000000-0000-0000-0000-000000000001", "prospect_id": "p0000000-0000-0000-0000-000000000004"},
            {"list_id": "l0000000-0000-0000-0000-000000000001", "prospect_id": "p0000000-0000-0000-0000-000000000005"},
            # Enterprise list - 5 prospects
            {"list_id": "l0000000-0000-0000-0000-000000000002", "prospect_id": "p0000000-0000-0000-0000-000000000006"},
            {"list_id": "l0000000-0000-0000-0000-000000000002", "prospect_id": "p0000000-0000-0000-0000-000000000007"},
            {"list_id": "l0000000-0000-0000-0000-000000000002", "prospect_id": "p0000000-0000-0000-0000-000000000008"},
            {"list_id": "l0000000-0000-0000-0000-000000000002", "prospect_id": "p0000000-0000-0000-0000-000000000009"},
            {"list_id": "l0000000-0000-0000-0000-000000000002", "prospect_id": "p0000000-0000-0000-0000-000000000010"},
            # SMB list - 5 prospects
            {"list_id": "l0000000-0000-0000-0000-000000000003", "prospect_id": "p0000000-0000-0000-0000-000000000011"},
            {"list_id": "l0000000-0000-0000-0000-000000000003", "prospect_id": "p0000000-0000-0000-0000-000000000012"},
            {"list_id": "l0000000-0000-0000-0000-000000000003", "prospect_id": "p0000000-0000-0000-0000-000000000013"},
            {"list_id": "l0000000-0000-0000-0000-000000000003", "prospect_id": "p0000000-0000-0000-0000-000000000014"},
            {"list_id": "l0000000-0000-0000-0000-000000000003", "prospect_id": "p0000000-0000-0000-0000-000000000015"},
        ]
        
        for member in list_members:
            existing = db.query(ProspectListMember).filter(
                ProspectListMember.list_id == member["list_id"],
                ProspectListMember.prospect_id == member["prospect_id"]
            ).first()
            if not existing:
                plm = ProspectListMember(id=str(uuid.uuid4()), **member)
                db.add(plm)
        print(f"[OK] Assigned prospects to lists")
        
        db.flush()
        
        # =====================
        # 6. Create Campaigns
        # =====================
        campaigns_data = [
            {
                "campaign_id": "c0000000-0000-0000-0000-000000000001",
                "campaign_name": "Winter Promo 2025",
                "status": "ACTIVE",
                "created_by": "00000000-0000-0000-0000-000000000001",
                "start_date": date(2025, 1, 15),
                "end_date": date(2025, 2, 15),
                "send_window_start": time(9, 0),
                "send_window_end": time(17, 0),
            },
            {
                "campaign_id": "c0000000-0000-0000-0000-000000000002",
                "campaign_name": "Product Launch - Q1",
                "status": "DRAFT",
                "created_by": "00000000-0000-0000-0000-000000000002",
                "start_date": date(2025, 2, 1),
                "end_date": date(2025, 3, 1),
            },
            {
                "campaign_id": "c0000000-0000-0000-0000-000000000003",
                "campaign_name": "Black Friday Outreach",
                "status": "COMPLETED",
                "created_by": "00000000-0000-0000-0000-000000000001",
                "start_date": date(2024, 11, 20),
                "end_date": date(2024, 11, 30),
            },
            {
                "campaign_id": "c0000000-0000-0000-0000-000000000004",
                "campaign_name": "Enterprise Demo Request",
                "status": "PAUSED",
                "created_by": "00000000-0000-0000-0000-000000000003",
                "start_date": date(2025, 1, 1),
                "end_date": date(2025, 1, 31),
            },
        ]
        
        for camp_data in campaigns_data:
            existing = db.query(Campaign).filter(Campaign.campaign_id == camp_data["campaign_id"]).first()
            if not existing:
                campaign = Campaign(
                    tenant_id=tenant_id,
                    respect_timezone=True,
                    **camp_data,
                )
                db.add(campaign)
                print(f"[OK] Created campaign: {camp_data['campaign_name']}")
        
        db.flush()
        
        # =====================
        # 4. Create Sequences
        # =====================
        sequences_data = [
            # Winter Promo - 3 step sequence
            {"sequence_id": "s0000000-0000-0000-0000-000000000001", "campaign_id": "c0000000-0000-0000-0000-000000000001", "step_number": 1, "wait_days": 0},
            {"sequence_id": "s0000000-0000-0000-0000-000000000002", "campaign_id": "c0000000-0000-0000-0000-000000000001", "step_number": 2, "wait_days": 3},
            {"sequence_id": "s0000000-0000-0000-0000-000000000003", "campaign_id": "c0000000-0000-0000-0000-000000000001", "step_number": 3, "wait_days": 5},
            # Product Launch - 2 step sequence
            {"sequence_id": "s0000000-0000-0000-0000-000000000004", "campaign_id": "c0000000-0000-0000-0000-000000000002", "step_number": 1, "wait_days": 0},
            {"sequence_id": "s0000000-0000-0000-0000-000000000005", "campaign_id": "c0000000-0000-0000-0000-000000000002", "step_number": 2, "wait_days": 4},
        ]
        
        for seq_data in sequences_data:
            existing = db.query(EmailSequence).filter(EmailSequence.sequence_id == seq_data["sequence_id"]).first()
            if not existing:
                seq = EmailSequence(
                    stop_on_reply=True,
                    stop_on_bounce=True,
                    send_start_hour=9,
                    send_end_hour=17,
                    **seq_data,
                )
                db.add(seq)
        print("[OK] Created email sequences")
        
        # =====================
        # 5. Create Templates
        # =====================
        templates_data = [
            {
                "template_id": "t0000000-0000-0000-0000-000000000001",
                "campaign_id": "c0000000-0000-0000-0000-000000000001",
                "sequence_id": "s0000000-0000-0000-0000-000000000001",
                "subject": "Special Winter Offer for {{company_name}}",
                "body": "Hi {{first_name}},\n\nI hope this email finds you well! I wanted to reach out about our exclusive winter promotion...\n\nBest regards,\nThe Team",
                "is_ai_generated": True,
                "tone": "professional",
            },
            {
                "template_id": "t0000000-0000-0000-0000-000000000002",
                "campaign_id": "c0000000-0000-0000-0000-000000000001",
                "sequence_id": "s0000000-0000-0000-0000-000000000002",
                "subject": "Following up on our Winter Offer",
                "body": "Hi {{first_name}},\n\nJust wanted to check if you had a chance to review our winter promotion...\n\nBest,\nThe Team",
                "is_ai_generated": True,
                "tone": "friendly",
            },
        ]
        
        for tmpl_data in templates_data:
            existing = db.query(EmailTemplate).filter(EmailTemplate.template_id == tmpl_data["template_id"]).first()
            if not existing:
                tmpl = EmailTemplate(**tmpl_data)
                db.add(tmpl)
        print("[OK] Created email templates")
        
        # =====================
        # 6. Create Metrics
        # =====================
        metrics_data = [
            {"campaign_id": "c0000000-0000-0000-0000-000000000001", "sent_count": 1250, "opened_count": 475, "replied_count": 87, "bounced_count": 23},
            {"campaign_id": "c0000000-0000-0000-0000-000000000002", "sent_count": 0, "opened_count": 0, "replied_count": 0, "bounced_count": 0},
            {"campaign_id": "c0000000-0000-0000-0000-000000000003", "sent_count": 5000, "opened_count": 2100, "replied_count": 312, "bounced_count": 145},
            {"campaign_id": "c0000000-0000-0000-0000-000000000004", "sent_count": 320, "opened_count": 98, "replied_count": 12, "bounced_count": 8},
        ]
        
        for metrics in metrics_data:
            existing = db.query(CampaignMetricsRealtime).filter(
                CampaignMetricsRealtime.campaign_id == metrics["campaign_id"]
            ).first()
            if not existing:
                m = CampaignMetricsRealtime(row_version=0, **metrics)
                db.add(m)
        print("[OK] Created campaign metrics")
        
        # =====================
        # 7. Create Audit Logs
        # =====================
        audit_data = [
            {"campaign_id": "c0000000-0000-0000-0000-000000000001", "user_id": "00000000-0000-0000-0000-000000000001", "action": "CREATE_CAMPAIGN"},
            {"campaign_id": "c0000000-0000-0000-0000-000000000001", "user_id": "00000000-0000-0000-0000-000000000001", "action": "LAUNCH_CAMPAIGN"},
            {"campaign_id": "c0000000-0000-0000-0000-000000000003", "user_id": "00000000-0000-0000-0000-000000000001", "action": "CREATE_CAMPAIGN"},
        ]
        
        for audit in audit_data:
            log = AuditLog(
                log_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                user_id=audit["user_id"],
                action=audit["action"],
                entity_type="campaign",
                entity_id=audit["campaign_id"],
            )
            db.add(log)
        print("[OK] Created audit logs")
        
        print("\n[OK] Database seeded successfully!")
        print("\nSummary:")
        print(f"  - 1 Tenant")
        print(f"  - {len(users_data)} Users")
        print(f"  - {len(campaigns_data)} Campaigns")
        print(f"  - {len(sequences_data)} Sequence Steps")
        print(f"  - {len(templates_data)} Templates")


if __name__ == "__main__":
    seed_database()
