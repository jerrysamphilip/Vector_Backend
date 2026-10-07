# app/services/campaign_service.py
"""
Backward-compatible re-export shim.

The original CampaignService has been split into three focused services:
  - CampaignCRUDService     → campaign_crud_service.py
  - CampaignEmailService    → campaign_email_service.py
  - CampaignAnalyticsService→ campaign_analytics_service.py

This module re-exports CampaignService as a thin composite that inherits from
all three services so that any existing callers importing CampaignService
continue to work without changes.
"""

from app.services.campaign_crud_service import CampaignCRUDService
from app.services.campaign_email_service import CampaignEmailService
from app.services.campaign_analytics_service import CampaignAnalyticsService
from sqlalchemy.orm import Session


class CampaignService(CampaignCRUDService):
    """
    Composite backward-compatible alias.

    Inherits all CRUD, state-management, and analytics methods from
    CampaignCRUDService (which in turn delegates analytics to
    CampaignAnalyticsService internally).

    Launch / enroll / stub-generation methods are proxied to
    CampaignEmailService so they remain callable via this class.
    """

    def __init__(self, db: Session):
        super().__init__(db)
        self._email_svc = CampaignEmailService(db)

    # ── Proxy email-related methods ──────────────────────────────────────────

    def launch(self, campaign_id: str, user_id: str):
        return self._email_svc.launch(campaign_id, user_id)

    def generate_stub_content(self, campaign_id: str, user_id: str) -> int:
        return self._email_svc.generate_stub_content(campaign_id, user_id)

    def enroll_prospects(self, campaign_id: str, user_id: str, request) -> int:
        return self._email_svc.enroll_prospects(campaign_id, user_id, request)

    def enroll_prospects_with_report(self, campaign_id: str, user_id: str, request):
        return self._email_svc.enroll_prospects_with_report(campaign_id, user_id, request)

    def remove_prospect(self, campaign_id: str, prospect_id: str, user_id: str) -> bool:
        return self._email_svc.remove_prospect(campaign_id, prospect_id, user_id)

    def _substitute_placeholders(self, template, prospect, sender_name=None, cta_link=None, sender_title=None) -> str:
        return self._email_svc.substitute_placeholders(template, prospect, sender_name, cta_link, sender_title)

    def _preschedule_all_emails(self, campaign_id: str) -> int:
        return self._email_svc._preschedule_all_emails(campaign_id)


# Expose the three specific services for new code that wants to import them
__all__ = [
    "CampaignService",
    "CampaignCRUDService",
    "CampaignEmailService",
    "CampaignAnalyticsService",
]
