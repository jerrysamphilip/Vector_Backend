
# app/services/deliverability_service.py
"""
Service for calculating and monitoring Domain Reputation and Deliverability.
"""
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.domain_reputation import SendingDomain, DomainHealthSnapshot, ReputationAlert
from app.models.email_message import EmailEvent
from app.models.campaign import Campaign
from app.models.sending_inbox import SendingInbox
from app.models.join_tables import campaign_inboxes

import boto3
import dns.resolver
from botocore.exceptions import ClientError
from app.core.config import settings
from app.utils.retry_utils import retry_aws_call

logger = logging.getLogger(__name__)

class DeliverabilityService:
    def __init__(self):
        client_kwargs = {"region_name": settings.AWS_REGION}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        
        self.ses_v2 = boto3.client("sesv2", **client_kwargs)
        self.ses_v1 = boto3.client("ses", **client_kwargs)

    def get_sending_statistics(self) -> dict:
        """
        Fetch sending statistics (Sends, Bounces, Complaints, Rejects) from AWS SES.
        Returns raw data points and calculated summaries.
        """
        try:
            @retry_aws_call
            def _get_send_statistics():
                return self.ses_v1.get_send_statistics()

            response = _get_send_statistics()
            data_points = response.get("SendDataPoints", [])
            
            # Sort by timestamp
            data_points.sort(key=lambda x: x["Timestamp"])
            
            # Calculate Summary
            total_attempts = sum(p["DeliveryAttempts"] for p in data_points)
            total_bounces = sum(p["Bounces"] for p in data_points)
            total_complaints = sum(p["Complaints"] for p in data_points)
            total_rejects = sum(p["Rejects"] for p in data_points)
            
            bounce_rate = (total_bounces / total_attempts) if total_attempts > 0 else 0.0
            complaint_rate = (total_complaints / total_attempts) if total_attempts > 0 else 0.0
            
            return {
                "data_points": data_points,
                "summary": {
                    "total_attempts": total_attempts,
                    "total_bounces": total_bounces,
                    "total_complaints": total_complaints,
                    "total_rejects": total_rejects,
                    "bounce_rate": round(bounce_rate * 100, 2), # Percentage
                    "complaint_rate": round(complaint_rate * 100, 2)
                }
            }
        except Exception as e:
            logger.error(f"[Deliverability] Failed to fetch stats: {e}")
            return {"data_points": [], "summary": {}}

    def get_domain_statistics(self, db: Session, domain: str) -> dict:
        """
        Calculate statistics from LOCAL database for a specific domain.
        Aggregates EmailMessage (Sent) and EmailEvent (Bounces, Complaints).
        """
        from datetime import timedelta
        from collections import defaultdict
        from app.models.email_message import EmailMessage, EmailEvent

        # 1. Initialize time range (Last 14 days to match SES)
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=14)
        
        # Helper to format key
        def get_date_key(dt):
            return dt.date().isoformat()

        stats_map = defaultdict(lambda: {"DeliveryAttempts": 0, "Bounces": 0, "Complaints": 0, "Rejects": 0})
        
        # Pre-fill last 14 days with 0s
        for i in range(15):
            d = start_date + timedelta(days=i)
            key = get_date_key(d)
            stats_map[key] # Access to init

        # 2. Count SENDS (Messages with status=SENT or REPLIED or BOUNCED or COMPLAINED)
        # We look at sent_at timestamp
        sent_query = db.query(
            func.date(EmailMessage.sent_at).label('date'), 
            func.count(EmailMessage.message_id).label('count')
        ).filter(
            EmailMessage.from_email.like(f"%@{domain}"),
            EmailMessage.sent_at >= start_date
        ).group_by(func.date(EmailMessage.sent_at)).all()

        for date_val, count in sent_query:
            if date_val:
                stats_map[date_val.isoformat()]["DeliveryAttempts"] += count

        # 3. Count EVENTS (Bounces, Complaints) linked to this domain
        # Join Events -> Messages to filter by domain
        event_query = db.query(
            func.date(EmailEvent.event_time).label('date'),
            EmailEvent.event_type,
            func.count(EmailEvent.event_id).label('count')
        ).join(
            EmailMessage, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.from_email.like(f"%@{domain}"),
            EmailEvent.event_time >= start_date,
            EmailEvent.event_type.in_([EmailEvent.EVENT_BOUNCE, EmailEvent.EVENT_UNSUBSCRIBE]) # Unsubscribe covers complaints roughly
        ).group_by(
            func.date(EmailEvent.event_time),
            EmailEvent.event_type
        ).all()

        for date_val, event_type, count in event_query:
            if not date_val: continue
            key = date_val.isoformat()
            
            if event_type == EmailEvent.EVENT_BOUNCE:
                stats_map[key]["Bounces"] += count
            elif event_type == EmailEvent.EVENT_UNSUBSCRIBE:
                # We'll treat Unsubscribes involving complaints as complaints
                # For simplicity in this view, we map Unsub -> Complaint visual line 
                # (Or strictly check metadata if we want exact 'Spam Complaint' vs 'Unsubscribe')
                stats_map[key]["Complaints"] += count

        # 4. Format for Response
        data_points = []
        total_attempts = 0
        total_bounces = 0
        total_complaints = 0

        sorted_keys = sorted(stats_map.keys())
        for k in sorted_keys:
            v = stats_map[k]
            data_points.append({
                "Timestamp": datetime.fromisoformat(k),
                "DeliveryAttempts": v["DeliveryAttempts"],
                "Bounces": v["Bounces"],
                "Complaints": v["Complaints"],
                "Rejects": v["Rejects"]
            })
            total_attempts += v["DeliveryAttempts"]
            total_bounces += v["Bounces"]
            total_complaints += v["Complaints"]

        bounce_rate = (total_bounces / total_attempts) if total_attempts > 0 else 0.0
        complaint_rate = (total_complaints / total_attempts) if total_attempts > 0 else 0.0

        return {
            "data_points": data_points,
            "summary": {
                "total_attempts": total_attempts,
                "total_bounces": total_bounces,
                "total_complaints": total_complaints,
                "total_rejects": 0,
                "bounce_rate": round(bounce_rate * 100, 2),
                "complaint_rate": round(complaint_rate * 100, 2)
            }
        }

    def sync_ses_metrics(self, db: Session):
        """
        Fetch account-level reputation and SESv2 status from AWS.
        Updates all domains with the current account health.
        """
        try:
            @retry_aws_call
            def _get_account():
                return self.ses_v2.get_account()

            response = _get_account()
            
            # Extract Metrics
            reputation = response.get("Reputation", {})
            # SESv2 reports HEALTHY / PROBATION / SHUTDOWN as EnforcementStatus
            account_status = response.get("EnforcementStatus") or reputation.get("ReputationStatus", "HEALTHY")
            if response.get("SendingEnabled") is False or account_status == "SHUTDOWN":
                # Nothing can be sent: pause active campaigns with the reason instead of
                # letting every send fail (BR-DF-07)
                from app.services.send_safety import auto_pause
                reason = f"Amazon SES sending is disabled for this account (status {account_status})"
                for campaign in db.query(Campaign).filter(Campaign.status == "ACTIVE").all():
                    auto_pause(db, campaign, reason)
            account_score = reputation.get("AccountReputationScore", 1.0)
            
            # Check if SESv2 is optimized (enabled for this account)
            sesv2_enabled = response.get("ProductionAccessEnabled", False)
            
            logger.info(f"[Deliverability] SESv2 Sync: Status={account_status}, Score={account_score}")
            
            # Update all domains (SES account reputation affects all senders in that account)
            domains = db.query(SendingDomain).all()
            for domain in domains:
                domain.ses_reputation_status = account_status
                domain.account_reputation_score = account_score
                domain.sesv2_enabled = sesv2_enabled
                domain.updated_at = datetime.utcnow()
            
            db.commit()
            return True
        except Exception as e:
            logger.error(f"[Deliverability] AWS SESv2 sync failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[Deliverability] Unexpected error in sync_ses_metrics: {e}")
            return False

    def update_domain_stats(self, domain_name: str, event_type: str, db: Session):
        """Kept for callers outside the webhook; health is now rate-based (send_safety)."""
        from app.services.send_safety import check_domain_health
        if event_type in ("BOUNCE", "COMPLAINT"):
            check_domain_health(db, domain_name, event_type.lower())
            return
        return self._legacy_update_domain_stats(domain_name, event_type, db)

    def _legacy_update_domain_stats(self, domain_name: str, event_type: str, db: Session):
        """
        Update reputation score based on real-time events (Bounce, Complaint).
        Triggers safety switch if score drops below threshold.
        """
        # 1. Get or Create Domain
        domain = db.query(SendingDomain).filter(SendingDomain.domain_name == domain_name).first()
        if not domain:
            domain = SendingDomain(domain_name=domain_name)
            db.add(domain)
            db.flush() # flush to get default values if any
        
        # 2. Adjust Score logic (Simplified for MVP)
        # Starting Score: 100. Bounce = -5, Complaint = -20. Open = +1.
        current_score = domain.current_reputation_score
        
        if event_type == "BOUNCE":
            current_score -= 5
        elif event_type == "COMPLAINT":
            current_score -= 20
        elif event_type == "OPEN":
            current_score += 1
        
        # Clamp score between 0 and 100
        domain.current_reputation_score = max(0, min(100, current_score))
        
        logger.info(f"[Deliverability] Domain {domain_name} score updated to {domain.current_reputation_score} (Event: {event_type})")
        
        # 3. Check Safety Switch
        if domain.current_reputation_score < domain.safety_threshold:
            self._trigger_safety_switch(domain, db)
            
        db.commit()

    def _trigger_safety_switch(self, domain: SendingDomain, db: Session):
        """
        PAUSE all active campaigns using this domain and alert.
        """
        # Create Alert
        alert = ReputationAlert(
            domain_name=domain.domain_name,
            alert_type="REPUTATION_DROP",
            severity="CRITICAL",
            details=f"Score dropped to {domain.current_reputation_score} (Threshold: {domain.safety_threshold}). Campaigns Paused."
        )
        db.add(alert)
        
        affected_campaigns = (
            db.query(Campaign)
            .join(campaign_inboxes, campaign_inboxes.c.campaign_id == Campaign.campaign_id)
            .join(SendingInbox, SendingInbox.inbox_id == campaign_inboxes.c.inbox_id)
            .filter(
                Campaign.status == "ACTIVE",
                SendingInbox.email_address.like(f"%@{domain.domain_name}"),
            )
            .all()
        )
        for campaign in affected_campaigns:
            campaign.status = "PAUSED"

        logger.warning(
            f"[SAFETY SWITCH] Paused {len(affected_campaigns)} campaign(s) for {domain.domain_name} due to low reputation!"
        )

    def create_snapshot(self, db: Session):
        """
        Scheduled job to create daily snapshots of all domains.
        """
        domains = db.query(SendingDomain).all()
        for d in domains:
            snap = DomainHealthSnapshot(
                domain_name=d.domain_name,
                reputation_score=d.current_reputation_score,
                # Calculate rates from raw logs in real implementation
                bounce_rate_24h=0.0, 
                complaint_rate_24h=0.0, 
                open_rate_24h=0.0
            )
            db.add(snap)
        db.commit()

    def sync_from_inboxes(self, db: Session):
        """
        Scan all SendingInboxes and ensure their domains are registered in the reputation system.
        Uses raw SQL to bypass SQLAlchemy column/relationship mismatch errors.
        """
        from sqlalchemy import text
        try:
            # 1. Get domains from inboxes
            inboxes_res = db.execute(text("SELECT email_address FROM sending_inboxes")).fetchall()
            unique_domains = set()
            for row in inboxes_res:
                email = row[0]
                if email and "@" in email:
                    unique_domains.add(email.split("@")[-1])
            
            # 2. Get existing domains
            existing_res = db.execute(text("SELECT domain_name FROM sending_domains")).fetchall()
            existing_domains = {r[0] for r in existing_res}
            
            # 3. Insert new domains
            synced_count = 0
            for d_name in unique_domains:
                if d_name not in existing_domains:
                    logger.info(f"[Deliverability] Syncing new domain from inbox: {d_name}")
                    db.execute(text(
                        "INSERT INTO sending_domains (domain_name, current_reputation_score, safety_threshold, warmup_status, spf_status, dkim_status, dmarc_status, is_blacklisted) "
                        "VALUES (:name, 100, 70, 'COMPLETED', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN', 0)"
                    ), {"name": d_name})
                    synced_count += 1
            
            db.commit()
            return synced_count
        except Exception as e:
            db.rollback()
            logger.error(f"[Deliverability] sync_from_inboxes raw SQL failed: {e}")
            raise e

    def perform_dns_scan(self, domain_name: str, db: Session) -> dict:
        """
        Perform real-time DNS queries to verify SPF and DMARC alignment.
        Also syncs DKIM status from AWS SES directly.
        Updates the SendingDomain record.
        """
        logger.info(f"[Deliverability] Starting DNS scan for {domain_name}")
        
        spf_status = "FAIL"
        dmarc_status = "FAIL"
        dkim_status = "UNKNOWN"

        # 1. Check SPF — SES uses Custom MAIL FROM subdomain (mail.<domain>) as envelope sender.
        #    SPF is evaluated against that subdomain, not the root domain.
        #    Check order: mail.<domain> first, then root domain.
        import re as _re

        def _spf_has_ses(check_domain: str) -> bool:
            """Return True if the given domain's SPF record authorises amazonses.com."""
            try:
                ans = dns.resolver.resolve(check_domain, 'TXT')
                for rd in ans:
                    txt = rd.to_text().strip('"')
                    if not txt.startswith("v=spf1"):
                        continue
                    if "amazonses.com" in txt:
                        return True
                    # Walk one level of includes
                    for inc in _re.findall(r"include:(\S+)", txt):
                        try:
                            inc_ans = dns.resolver.resolve(inc, 'TXT')
                            for inc_rd in inc_ans:
                                if "amazonses.com" in inc_rd.to_text():
                                    return True
                        except Exception:
                            pass
            except Exception:
                pass
            return False

        try:
            mail_from_subdomain = f"mail.{domain_name}"
            if _spf_has_ses(mail_from_subdomain):
                # Correct SES Custom MAIL FROM setup — SPF passes via subdomain
                spf_status = "PASS"
                logger.info(
                    f"[Deliverability] SPF PASS for {domain_name} via Custom MAIL FROM "
                    f"subdomain ({mail_from_subdomain} includes amazonses.com)"
                )
            elif _spf_has_ses(domain_name):
                # Root domain includes amazonses.com directly
                spf_status = "PASS"
            else:
                spf_status = "WARNING"
                logger.warning(
                    f"[Deliverability] SPF WARNING for {domain_name}: neither "
                    f"{mail_from_subdomain} nor root domain authorises amazonses.com. "
                    f"Add 'v=spf1 include:amazonses.com ~all' to {mail_from_subdomain} TXT record."
                )
        except Exception as e:
            logger.info(f"[Deliverability] SPF check failed for {domain_name}: {e}")

        # 2. Check DMARC (warn on p=none — no enforcement)
        try:
            answers = dns.resolver.resolve(f"_dmarc.{domain_name}", 'TXT')
            for rdata in answers:
                txt_record = rdata.to_text().strip('"')
                if txt_record.startswith("v=DMARC1"):
                    if "p=none" in txt_record:
                        dmarc_status = "WARNING"
                        logger.warning(
                            f"[Deliverability] DMARC exists for {domain_name} but "
                            f"policy is 'none' (no enforcement). Consider p=quarantine or p=reject."
                        )
                    else:
                        dmarc_status = "PASS"
                    break
        except Exception as e:
            logger.info(f"[Deliverability] DMARC check failed for {domain_name}: {e}")
            
        # 3. Check DKIM via AWS SES (Source of Truth)
        try:
            # We can't DNS scan DKIM without selector, so we ask AWS if it's happy.
            response = self.ses_v1.get_identity_dkim_attributes(Identities=[domain_name])
            dkim_attrs = response.get("DkimAttributes", {}).get(domain_name, {})
            verification_status = dkim_attrs.get("DkimVerificationStatus")
            
            if verification_status == "Success":
                dkim_status = "PASS"
            elif verification_status == "Pending":
                dkim_status = "WARNING"
            elif verification_status == "Failed":
                dkim_status = "FAIL"
                
        except Exception as e:
            logger.info(f"[Deliverability] AWS DKIM check failed for {domain_name}: {e}")

            
        # 4. Update Database
        domain = db.query(SendingDomain).filter(SendingDomain.domain_name == domain_name).first()
        if domain:
            domain.spf_status = spf_status
            domain.dmarc_status = dmarc_status
            domain.dkim_status = dkim_status
            domain.updated_at = datetime.utcnow()
            db.commit()
            
        # Build actionable warnings for the UI
        warnings = []
        if spf_status == "WARNING":
            warnings.append(
                f"SPF is not configured for SES. "
                f"Create a TXT record on mail.{domain_name} with value: "
                f"'v=spf1 include:amazonses.com ~all' (SES Custom MAIL FROM subdomain)."
            )
        if dmarc_status == "WARNING":
            warnings.append(
                "DMARC policy is set to 'none' (no enforcement). "
                "Upgrade to p=quarantine or p=reject for better deliverability."
            )

        return {
            "domain": domain_name,
            "spf": spf_status,
            "dmarc": dmarc_status,
            "dkim": dkim_status,
            "warnings": warnings,
        }

    def get_all_domains_enriched(self, db: Session) -> list:
        """
        Fetch all domains and populate associated inboxes and campaigns.
        """
        domains = db.query(SendingDomain).all()
        results = []

        for d in domains:
            # 1. Find Inboxes for this domain
            inboxes = db.query(SendingInbox).filter(
                SendingInbox.email_address.like(f"%@{d.domain_name}")
            ).all()

            inbox_emails = [i.email_address for i in inboxes]
            
            # 2. Find Active Campaigns linked to these inboxes
            # Campaign -> campaign_inboxes -> SendingInbox
            active_campaign_names = set()
            for inbox in inboxes:
                # Assuming 'campaigns' relationship exists on SendingInbox
                for campaign in inbox.campaigns:
                    if campaign.status == "ACTIVE":
                        active_campaign_names.add(campaign.campaign_name)
            
            # Convert to Dictionary to match Pydantic Schema
            domain_dict = {
                "domain_name": d.domain_name,
                "spf_status": d.spf_status,
                "dkim_status": d.dkim_status,
                "dmarc_status": d.dmarc_status,
                "current_reputation_score": d.current_reputation_score,
                "safety_threshold": d.safety_threshold,
                "is_blacklisted": d.is_blacklisted,
                "warmup_status": d.warmup_status,
                "ses_reputation_status": d.ses_reputation_status,
                "sesv2_enabled": d.sesv2_enabled,
                "account_reputation_score": d.account_reputation_score,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
                "associated_inboxes": inbox_emails,
                "active_campaigns": list(active_campaign_names)
            }
            results.append(domain_dict)
            
        return results

    def verify_domain_and_get_tokens(self, domain_name: str, db: Session) -> dict:
        """
        Initiate DKIM verification for a domain and return the DNS records needed.
        Also ensures the domain is tracked in SendingDomain table.
        """
        logger.info(f"[Deliverability] Verifying domain identity: {domain_name}")
        
        dns_records = []
        dkim_tokens = []
        
        try:
            # 1. Trigger AWS Verification / Get Tokens
            # Using verify_domain_dkim (SES V1) as it directly gives tokens
            response = self.ses_v1.verify_domain_dkim(Domain=domain_name)
            dkim_tokens = response.get("DkimTokens", [])
            
            # Format as DNS records
            for token in dkim_tokens:
                dns_records.append({
                    "type": "CNAME",
                    "host": f"{token}._domainkey.{domain_name}",
                    "value": f"{token}.dkim.amazonses.com",
                    "status": "Verify in DNS"
                })
            
            # Add SPF Record (Generic for SES)
            dns_records.append({
                "type": "TXT",
                "host": "@" if "." in domain_name else domain_name, # or just empty string depending on provider
                "value": "v=spf1 include:amazonses.com ~all",
                "status": "Recommended"
            })

            # Add DMARC Record (Generic safe start)
            dns_records.append({
                "type": "TXT",
                "host": f"_dmarc.{domain_name}",
                "value": "v=DMARC1; p=none;",
                "status": "Recommended"
            })
                
            # 2. Ensure Domain Exists in DB
            domain = db.query(SendingDomain).filter(SendingDomain.domain_name == domain_name).first()
            if not domain:
                domain = SendingDomain(
                    domain_name=domain_name,
                    spf_status="UNKNOWN",
                    dkim_status="PENDING", # We just asked for tokens
                    dmarc_status="UNKNOWN",
                    warmup_status="WARMING", # Start in warmup
                    safety_threshold=70
                )
                db.add(domain)
            else:
                # If existed, maybe update status?
                if domain.dkim_status != "PASS":
                     domain.dkim_status = "PENDING"
            
            db.commit()
            
            return {
                "domain": domain_name,
                "dkim_tokens": dkim_tokens,
                "dns_records": dns_records,
                "verification_status": "PENDING"
            }
            
        except Exception as e:
            logger.error(f"[Deliverability] Failed to verify domain {domain_name}: {e}")
            raise e

deliverability_service = DeliverabilityService()
