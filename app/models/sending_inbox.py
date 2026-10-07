# app/models/sending_inbox.py
"""
Sending Inbox model for SMTP email accounts.
"""

from sqlalchemy import Column, String, Boolean, Integer, TIMESTAMP, ForeignKey, Float, Text
from app.core.encrypted_type import EncryptedString, EncryptedText
from sqlalchemy.orm import relationship
import uuid

from app.models.base import Base


class SendingInbox(Base):
    """
    SMTP sending inbox/email account.
    Supports daily limits and cooling periods for deliverability.
    """
    __tablename__ = "sending_inboxes"

    inbox_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    
    email_address = Column(String(255), nullable=False)
    provider = Column(String(50), nullable=True)  # gmail / sendgrid / outlook / etc
    
    # SMTP Configuration (per-inbox sending credentials)
    smtp_host = Column(String(255), nullable=True)
    smtp_port = Column(Integer, default=587)
    smtp_username = Column(String(255), nullable=True)
    smtp_password = Column(EncryptedString, nullable=True)  # encrypted at rest
    smtp_use_ssl = Column(Boolean, default=False)   # False=STARTTLS(587), True=SSL(465)

    # Mailbox sign-in (BR-DF-05): PASSWORD, or OAUTH_MS365 for Microsoft 365 mailboxes
    # where basic auth is blocked; IMAP/SMTP then authenticate with XOAUTH2.
    auth_type = Column(String(30), default="PASSWORD")
    oauth_refresh_token = Column(EncryptedText, nullable=True)
    oauth_access_token = Column(EncryptedText, nullable=True)
    oauth_expires_at = Column(TIMESTAMP, nullable=True)
    oauth_error = Column(Text, nullable=True)
    imap_last_error = Column(Text, nullable=True)   # last reply-sync failure, shown on the inbox

    # IMAP Configuration
    imap_host = Column(String(255), nullable=True)
    imap_port = Column(Integer, default=993)
    imap_username = Column(String(255), nullable=True)
    imap_password = Column(EncryptedString, nullable=True)  # encrypted at rest
    last_sync_at = Column(TIMESTAMP, nullable=True)
    
    # Rate Limiting
    # 500/day is the post-warmup baseline this application is sized for (see
    # current_daily_limit below) — verify against the AWS SES account's
    # Max24HourSend / MaxSendRate before relying on it for a given tenant.
    daily_limit = Column(Integer, default=500)
    cooling_period_hours = Column(Integer, default=24)

    # Warm-up & Throttle Settings (user-configurable)
    # Defaults to False: mailboxes send at daily_limit immediately rather than
    # ramping. The ramp (WARMUP_SCHEDULE below) still exists and can be
    # switched on per-mailbox for a brand-new domain that needs to build
    # reputation gradually — it's just no longer the default.
    warmup_enabled = Column(Boolean, default=False)
    warmup_day = Column(Integer, default=0)                    # Current warmup day (0 = day of creation)
    warmup_start_date = Column(TIMESTAMP, nullable=True)       # When warmup started
    max_emails_per_day = Column(Integer, nullable=True)        # User override (null = use warmup schedule)
    # Seconds between emails for this inbox. 60s supports up to 1440/day over a
    # full 24h day; a restricted campaign send_window shrinks that ceiling
    # (window_seconds / delay_between_emails) — see current_daily_limit.
    delay_between_emails = Column(Integer, default=60)
    emails_sent_today = Column(Integer, default=0)             # Counter reset daily
    last_daily_reset = Column(TIMESTAMP, nullable=True)        # When counter was last reset

    # Warm-up operations settings / health
    warmup_status = Column(String(50), default="ACTIVE")       # ACTIVE / PAUSED / ISSUE / COOLING / DISABLED
    warmup_pool = Column(String(50), default="FOUNDATION")     # FOUNDATION / GROWTH / PREMIUM / ULTRA_PREMIUM
    warmup_reputation = Column(Float, default=65.0)
    warmup_auto_adjust = Column(Boolean, default=True)
    warmup_randomize = Column(Boolean, default=True)
    warmup_reply_rate_target = Column(Integer, default=35)
    warmup_max_target = Column(Integer, nullable=True)
    warmup_daily_target = Column(Integer, default=0)
    warmup_identifier = Column(String(120), nullable=True)
    warmup_issue_code = Column(String(100), nullable=True)
    warmup_issue_message = Column(Text, nullable=True)
    warmup_last_activity_at = Column(TIMESTAMP, nullable=True)
    warmup_today_sent = Column(Integer, default=0)
    warmup_today_opened = Column(Integer, default=0)
    warmup_today_replied = Column(Integer, default=0)
    warmup_today_saved = Column(Integer, default=0)

    # Status
    last_sent_at = Column(TIMESTAMP, nullable=True)
    is_in_cooling = Column(Boolean, default=False)
    status = Column(String(50), default="ACTIVE")  # ACTIVE / PAUSED / WARMING

    # Relationships
    tenant = relationship("Tenant", back_populates="sending_inboxes")
    messages = relationship("EmailMessage", back_populates="inbox", lazy="dynamic")
    campaigns = relationship("Campaign", secondary="campaign_inboxes", back_populates="inboxes")
    warmup_metrics = relationship("InboxWarmupMetric", back_populates="inbox", lazy="dynamic")
    warmup_events = relationship(
        "InboxWarmupEvent",
        foreign_keys="InboxWarmupEvent.inbox_id",
        back_populates="inbox",
        lazy="dynamic"
    )

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    @property
    def current_daily_limit(self):
        """
        Calculate effective daily limit based on warmup schedule.
        """
        # User override takes priority
        if self.max_emails_per_day is not None:
            return self.max_emails_per_day

        # Warmup is off by default (see column default above) — mailboxes
        # send at daily_limit immediately with no ramp.
        if not self.warmup_enabled:
            return self.daily_limit

        # Warmup schedule implementation — opt-in per mailbox.
        # (Moved from service to model for UI visibility)
        # Days 0-9: ramp schedule (unchanged — this is the deliverability-
        # protecting curve for a brand-new mailbox/domain building sender
        # reputation; raising these early-day numbers is a reputation risk,
        # not just a config change, so it is left alone).
        # Day 10+: full daily_limit (graduation) — this is the number to raise
        # for higher sustained volume once a mailbox has graduated, or set
        # max_emails_per_day to override the ramp entirely for an
        # already-established mailbox/domain.
        WARMUP_SCHEDULE = [5, 8, 12, 18, 25, 35, 50, 70, 90, 120]

        day = self.warmup_day or 0
        if day >= len(WARMUP_SCHEDULE):
            # Graduated — use the user-configured daily_limit as the cap.
            # Ensure the graduated value is never less than the last ramp step (120)
            # so adding more daily_limit capacity never causes a regression.
            return max(self.daily_limit or 500, WARMUP_SCHEDULE[-1])

        return WARMUP_SCHEDULE[day]

    @property
    def warmup_effective_target(self):
        """
        Effective warmup target for the current day before auto-adjustment.
        """
        target = self.warmup_max_target or self.current_daily_limit or self.daily_limit or 0
        return max(0, min(target, self.daily_limit or target))

    def __repr__(self):
        return f"<SendingInbox {self.email_address}>"
