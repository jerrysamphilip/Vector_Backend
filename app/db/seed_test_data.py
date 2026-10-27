"""
Seed Test Data for Campaign View Testing
Adds templates, prospects, and analytics data to existing campaigns.
"""
import asyncio
import sys
import logging

logger = logging.getLogger(__name__)
sys.path.insert(0, '.')

from datetime import timedelta
import uuid

from sqlalchemy.orm import Session
from app.core.clock import utcnow
from app.core.database import SessionLocal
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.models.metrics import CampaignMetricsRealtime
from app.models.email_message import EmailMessage


def seed_test_data():
    """Add test data to existing campaigns."""
    from app.db.seed_guard import require_dev_environment
    require_dev_environment("seed_test_data")
    db = SessionLocal()
    
    try:
        # Find all campaigns
        campaigns = db.query(Campaign).all()
        
        if not campaigns:
            logger.info("No campaigns found! Create a campaign first.")
            return
        
        logger.info("Found %s campaigns", len(campaigns))
        
        for campaign in campaigns:
            logger.info("\n--- Processing: %s (%s...) ---", campaign.campaign_name, campaign.campaign_id[:8])
            
            # 1. Get sequences for this campaign
            sequences = db.query(EmailSequence).filter(
                EmailSequence.campaign_id == campaign.campaign_id
            ).order_by(EmailSequence.step_number).all()
            
            logger.info("  Found %s sequence steps", len(sequences))
            
            # 2. Create templates for each sequence step
            templates_created = 0
            for seq in sequences:
                existing = db.query(EmailTemplate).filter(
                    EmailTemplate.sequence_id == seq.sequence_id
                ).first()
                
                if existing:
                    logger.info("  Step %s: Template already exists", seq.step_number)
                    continue
                
                template = EmailTemplate(
                    template_id=str(uuid.uuid4()),
                    campaign_id=campaign.campaign_id,
                    sequence_id=seq.sequence_id,
                    designation="General",
                    subject=f"[Step {seq.step_number}] Introduction to Our Solution - {campaign.campaign_name}",
                    body=f"""Hi {{{{first_name}}}},

I noticed that {{{{company_name}}}} is making waves in the industry, and I wanted to reach out.

At our company, we help businesses like yours streamline their operations and boost productivity. Our solution has helped companies achieve:

• 30% faster lead response times
• 45% improvement in email engagement
• 2x more conversions from outreach campaigns

Would you be open to a quick 15-minute call this week to explore how we might help {{{{company_name}}}}?

Best regards,
{{your_name}}

---
This is email step {seq.step_number} of the "{campaign.campaign_name}" campaign.
""",
                    tone="professional",
                    ai_model="gpt-4o-mini",
                    is_ai_generated=True,
                    generated_at=utcnow(),
                    personalization_tokens=["{{first_name}}", "{{company_name}}"],
                    created_at=utcnow()
                )
                db.add(template)
                templates_created += 1
            
            logger.info("  Created %s new templates", templates_created)
            
            # 3. Create test prospects if none exist
            existing_prospects = db.query(CampaignProspect).filter(
                CampaignProspect.campaign_id == campaign.campaign_id
            ).count()
            
            if existing_prospects == 0:
                logger.info("  Adding test prospects...")
                
                # Create some prospect records
                test_prospects = [
                    {"first_name": "Alice", "last_name": "Johnson", "email": "alice.johnson@techcorp.com", "company_name": "TechCorp"},
                    {"first_name": "Bob", "last_name": "Williams", "email": "bob.williams@startup.io", "company_name": "Startup IO"},
                    {"first_name": "Carol", "last_name": "Davis", "email": "carol.davis@enterprise.co", "company_name": "Enterprise Co"},
                    {"first_name": "David", "last_name": "Brown", "email": "david.brown@growthco.com", "company_name": "Growth Co"},
                    {"first_name": "Eva", "last_name": "Martinez", "email": "eva.martinez@scaleup.org", "company_name": "ScaleUp Org"},
                ]
                
                tenant_id = campaign.tenant_id
                
                for p_data in test_prospects:
                    # Check if prospect already exists
                    existing = db.query(Prospect).filter(Prospect.email == p_data["email"]).first()
                    
                    if not existing:
                        prospect = Prospect(
                            prospect_id=str(uuid.uuid4()),
                            tenant_id=tenant_id,
                            first_name=p_data["first_name"],
                            last_name=p_data["last_name"],
                            email=p_data["email"],
                            company_name=p_data["company_name"],
                            designation="Manager",
                            consent_status="OPT_IN",
                        )
                        db.add(prospect)
                        db.flush()
                        existing = prospect
                    
                    # Enroll prospect in campaign
                    campaign_prospect = CampaignProspect(
                        campaign_id=campaign.campaign_id,
                        prospect_id=existing.prospect_id,
                        enrolled_at=utcnow(),
                        status="ACTIVE"
                    )
                    db.add(campaign_prospect)
                
                logger.info("  Added %s test prospects", len(test_prospects))
            else:
                logger.info("  Campaign already has %s prospects", existing_prospects)
            
            # 4. Update metrics
            metrics = db.query(CampaignMetricsRealtime).filter(
                CampaignMetricsRealtime.campaign_id == campaign.campaign_id
            ).first()
            
            if not metrics:
                metrics = CampaignMetricsRealtime(
                    campaign_id=campaign.campaign_id,
                    sent_count=0,
                    opened_count=0,
                    replied_count=0,
                    bounced_count=0,
                )
                db.add(metrics)
            
            # Add some test metrics
            prospect_count = db.query(CampaignProspect).filter(
                CampaignProspect.campaign_id == campaign.campaign_id
            ).count()
            
            metrics.sent_count = max(metrics.sent_count, prospect_count * 2)
            metrics.opened_count = max(metrics.opened_count, int(prospect_count * 1.2))
            metrics.replied_count = max(metrics.replied_count, int(prospect_count * 0.3))
            
            logger.info("  Updated metrics: %s sent, %s opened, %s replied", metrics.sent_count, metrics.opened_count, metrics.replied_count)
        
        db.commit()
        logger.info("\n✅ Test data seeded successfully!")
        
    except Exception as e:
        db.rollback()
        logger.exception("\n❌ Error: %s", e)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # CLI: progress lines on the console
    seed_test_data()
