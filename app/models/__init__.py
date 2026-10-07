# app/models/__init__.py
"""
SQLAlchemy models for Outreach AI.
All models use CHAR(36) for UUID primary keys.
"""

from app.models.base import Base

# Import all models to register them with Base and resolve relationships
# These imports must be after Base is defined
from app.models.tenant import Tenant
from app.models.user import User
from app.models.prospect import Prospect, GlobalUnsubscribe
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.account import Account
from app.models.contact_activity import ContactActivity
from app.models.contact_field import ContactFieldDefinition
from app.models.crm import PropertyChange, CrmTask, SavedView, ImportJob
from app.models.sales import Lead, SalesStage, Opportunity, Proposal
from app.models.campaign import Campaign, CampaignStateEvent, CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.email_template import AIPrompt, EmailTemplate, EmailTemplateVersion
from app.models.email_attachment import EmailAttachment
from app.models.sending_inbox import SendingInbox
from app.models.inbox_warmup_metric import InboxWarmupMetric
from app.models.inbox_warmup_event import InboxWarmupEvent
from app.models.email_message import EmailMessage, EmailEvent
from app.models.campaign_group import CampaignGroup, CampaignGroupMember
from app.models.metrics import CampaignMetricsRealtime
from app.models.audit import AuditLog
from app.models.persona_blueprint import PersonaBlueprint
from app.models.prospect_persona import ProspectPersona
from app.models.conversation import Conversation
from app.models.compliance_check import ComplianceCheck
from app.models.company_profile import CompanyProfile
from app.models.automation_rule import AutomationRule
from app.models.campaign_draft import CampaignDraft
from app.models.invitation import Invitation
from app.models.refresh_token import RefreshToken
from app.models.password_reset_token import PasswordResetToken
from app.models.magic_login_token import MagicLoginToken
from app.models.join_tables import campaign_inboxes
from app.models.domain_reputation import (
    SendingDomain,
    DomainHealthSnapshot,
    ReputationAlert,
)
from app.models.provider_reputation import (
    ExternalReputationMetric,
    ExternalFeedbackEvent,
    ExternalIngestionRun,
)
from app.models.alert_center import AlertPreference, AlertEmailDispatch

__all__ = [
    "Base",
    "Tenant",
    "User",
    "Prospect",
    "GlobalUnsubscribe",
    "ProspectList",
    "ProspectListMember",
    "Account",
    "ContactActivity",
    "ContactFieldDefinition",
    "PropertyChange",
    "CrmTask",
    "SavedView",
    "ImportJob",
    "Campaign",
    "CampaignStateEvent",
    "CampaignProspect",
    "EmailSequence",
    "AIPrompt",
    "EmailTemplate",
    "EmailTemplateVersion",
    "EmailAttachment",
    "SendingInbox",
    "InboxWarmupMetric",
    "InboxWarmupEvent",
    "EmailMessage",
    "EmailEvent",
    "CampaignGroup",
    "CampaignGroupMember",
    "CampaignMetricsRealtime",
    "AuditLog",
    "PersonaBlueprint",
    "ProspectPersona",
    "Conversation",
    "ComplianceCheck",
    "CompanyProfile",
    "AutomationRule",
    "CampaignDraft",
    "ExternalReputationMetric",
    "ExternalFeedbackEvent",
    "ExternalIngestionRun",
    "AlertPreference",
    "AlertEmailDispatch",
    "Invitation",
    "RefreshToken",
    "PasswordResetToken",
    "MagicLoginToken",
]
