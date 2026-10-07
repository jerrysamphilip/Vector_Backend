"""
Unified dashboard alert aggregation + alert email dispatching.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert_center import AlertEmailDispatch, AlertPreference
from app.models.campaign import Campaign
from app.models.domain_reputation import ReputationAlert
from app.models.sending_inbox import SendingInbox
from app.models.user import User
from app.services.email_sender_service import email_sender

logger = logging.getLogger(__name__)


ALERT_TYPE_DOMAIN_REPUTATION = "DOMAIN_REPUTATION"
ALERT_TYPE_INBOX_PAUSED = "INBOX_PAUSED"
ALERT_TYPE_INBOX_SYNC_STALE = "INBOX_SYNC_STALE"
ALERT_TYPE_CAMPAIGN_PAUSED = "CAMPAIGN_PAUSED"

ALL_ALERT_TYPES = (
    ALERT_TYPE_DOMAIN_REPUTATION,
    ALERT_TYPE_INBOX_PAUSED,
    ALERT_TYPE_INBOX_SYNC_STALE,
    ALERT_TYPE_CAMPAIGN_PAUSED,
)

INBOX_ALERT_TYPES = (
    ALERT_TYPE_INBOX_PAUSED,
    ALERT_TYPE_INBOX_SYNC_STALE,
)

PREF_SCOPE_USER = "USER"
PREF_SCOPE_USER_INBOX = "USER_INBOX"
LEGACY_SCOPE_TENANT = "TENANT"
LEGACY_SCOPE_INBOX = "INBOX"

DEFAULT_PREFS = {
    ALERT_TYPE_DOMAIN_REPUTATION: {"enabled": True, "email_enabled": False, "cooldown_minutes": 360},
    ALERT_TYPE_INBOX_PAUSED: {"enabled": True, "email_enabled": False, "cooldown_minutes": 360},
    ALERT_TYPE_INBOX_SYNC_STALE: {"enabled": True, "email_enabled": False, "cooldown_minutes": 360},
    ALERT_TYPE_CAMPAIGN_PAUSED: {"enabled": True, "email_enabled": False, "cooldown_minutes": 360},
}


class AlertCenterService:
    @staticmethod
    def _user_inbox_scope_id(user_id: str, inbox_id: str) -> str:
        # Keep scope_id within varchar(36) by storing a deterministic UUID.
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{user_id}:{inbox_id}"))

    def _tenant_domain_set(self, db: Session, tenant_id: str) -> set[str]:
        rows = (
            db.query(SendingInbox.email_address)
            .filter(SendingInbox.tenant_id == tenant_id)
            .all()
        )
        domains = set()
        for (email,) in rows:
            if email and "@" in email:
                domains.add(email.split("@", 1)[1].lower())
        return domains

    def _scope_key(self, scope_type: str, scope_id: Optional[str], alert_type: str) -> str:
        return f"{scope_type}:{scope_id or ''}:{alert_type}"

    def _pref_map(self, db: Session, tenant_id: str) -> Dict[str, AlertPreference]:
        prefs = db.query(AlertPreference).filter(AlertPreference.tenant_id == tenant_id).all()
        return {
            self._scope_key(p.scope_type, p.scope_id, p.alert_type): p
            for p in prefs
        }

    def _resolve_pref(
        self,
        pref_map: Dict[str, AlertPreference],
        *,
        alert_type: str,
        scope_type: str,
        scope_id: Optional[str],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if scope_type == "INBOX" and user_id and scope_id:
            specific_key = self._scope_key(
                PREF_SCOPE_USER_INBOX,
                self._user_inbox_scope_id(user_id, scope_id),
                alert_type,
            )
            specific = pref_map.get(specific_key)
            if specific:
                return {
                    "enabled": bool(specific.enabled),
                    "email_enabled": bool(specific.email_enabled),
                    "cooldown_minutes": int(specific.cooldown_minutes or 360),
                }

        if user_id:
            user_key = self._scope_key(PREF_SCOPE_USER, user_id, alert_type)
            user_pref = pref_map.get(user_key)
            if user_pref:
                return {
                    "enabled": bool(user_pref.enabled),
                    "email_enabled": bool(user_pref.email_enabled),
                    "cooldown_minutes": int(user_pref.cooldown_minutes or 360),
                }

        return dict(DEFAULT_PREFS.get(alert_type, {"enabled": True, "email_enabled": False, "cooldown_minutes": 360}))

    @staticmethod
    def _severity_rank(severity: str) -> int:
        sev = (severity or "").upper()
        if sev == "CRITICAL":
            return 0
        if sev == "WARNING":
            return 1
        return 2

    def collect_dashboard_alerts(self, db: Session, tenant_id: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []
        allowed_domains = self._tenant_domain_set(db, tenant_id)

        if allowed_domains:
            reputation_alerts = (
                db.query(ReputationAlert)
                .filter(
                    ReputationAlert.domain_name.in_(allowed_domains),
                    ReputationAlert.is_resolved == False,  # noqa: E712
                )
                .order_by(ReputationAlert.created_at.desc())
                .all()
            )
            for alert in reputation_alerts:
                alerts.append(
                    {
                        "id": f"deliverability:{alert.alert_id}",
                        "source": "DOMAIN",
                        "severity": (alert.severity or "WARNING").upper(),
                        "title": f"{(alert.alert_type or 'Alert').replace('_', ' ').title()} on {alert.domain_name}",
                        "message": alert.details or "Deliverability issue detected on this sending domain.",
                        "created_at": alert.created_at,
                        "route": "/app/domain-health",
                        "action_label": "Open Domain Health",
                        "alert_type": ALERT_TYPE_DOMAIN_REPUTATION,
                        "scope_type": "TENANT",
                        "scope_id": None,
                    }
                )

        inboxes = db.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id).all()
        now_utc = datetime.utcnow()
        for inbox in inboxes:
            inbox_status = (inbox.status or "").upper()
            if inbox_status != "ACTIVE":
                if inbox_status == "PAUSED":
                    title = "Suspended Alerts (Email)"
                    message = f"{inbox.email_address} is paused and will not send campaign emails until resumed."
                    severity = "WARNING"
                else:
                    title = "Inbox unavailable for campaign sending"
                    message = (
                        f"{inbox.email_address} is in {inbox_status or 'UNKNOWN'} state. "
                        "Campaign creation will reject this inbox until it returns to ACTIVE."
                    )
                    severity = "CRITICAL"

                alerts.append(
                    {
                        "id": f"email-status:{inbox.inbox_id}:{inbox_status}",
                        "source": "EMAIL",
                        "severity": severity,
                        "title": title,
                        "message": message,
                        "created_at": inbox.last_sent_at or inbox.last_sync_at or now_utc,
                        "route": "/app/email-accounts",
                        "action_label": "Open Email Accounts",
                        "alert_type": ALERT_TYPE_INBOX_PAUSED,
                        "scope_type": "INBOX",
                        "scope_id": inbox.inbox_id,
                    }
                )

            if inbox.last_sync_at:
                stale_hours = (now_utc - inbox.last_sync_at).total_seconds() / 3600
                if stale_hours >= 48:
                    alerts.append(
                        {
                            "id": f"email-sync:{inbox.inbox_id}:{inbox.last_sync_at.isoformat()}",
                            "source": "SYNC",
                            "severity": "CRITICAL" if stale_hours >= 96 else "WARNING",
                            "title": "Inbox sync is stale",
                            "message": f"{inbox.email_address} has not synced replies for {int(stale_hours)} hours.",
                            "created_at": inbox.last_sync_at,
                            "route": "/app/email-accounts",
                            "action_label": "Review inbox sync",
                            "alert_type": ALERT_TYPE_INBOX_SYNC_STALE,
                            "scope_type": "INBOX",
                            "scope_id": inbox.inbox_id,
                        }
                    )

        paused_campaigns = (
            db.query(Campaign)
            .filter(Campaign.tenant_id == tenant_id, Campaign.status == "PAUSED")
            .all()
        )
        for campaign in paused_campaigns:
            alerts.append(
                {
                    "id": f"campaign-paused:{campaign.campaign_id}:{campaign.updated_at.isoformat() if campaign.updated_at else ''}",
                    "source": "CAMPAIGN",
                    "severity": "INFO",
                    "title": "Campaign activity is paused",
                    "message": f"{campaign.campaign_name} is paused and not progressing until it is resumed.",
                    "created_at": campaign.updated_at,
                    "route": f"/app/campaigns/{campaign.campaign_id}",
                    "action_label": "Open campaign",
                    "alert_type": ALERT_TYPE_CAMPAIGN_PAUSED,
                    "scope_type": "TENANT",
                    "scope_id": None,
                }
            )

        pref_map = self._pref_map(db, tenant_id)
        filtered_alerts: List[Dict[str, Any]] = []
        for alert in alerts:
            pref = self._resolve_pref(
                pref_map,
                alert_type=alert["alert_type"],
                scope_type=alert["scope_type"],
                scope_id=alert["scope_id"],
                user_id=user_id,
            )
            if not pref["enabled"]:
                continue
            alert["email_enabled"] = pref["email_enabled"]
            alert["cooldown_minutes"] = pref["cooldown_minutes"]
            filtered_alerts.append(alert)

        filtered_alerts.sort(
            key=lambda a: (
                self._severity_rank(a.get("severity", "INFO")),
                -(a.get("created_at").timestamp() if a.get("created_at") else 0),
            )
        )
        return filtered_alerts

    def list_preferences(self, db: Session, tenant_id: str, user_id: str) -> Dict[str, Any]:
        pref_map = self._pref_map(db, tenant_id)

        tenant_preferences = []
        for alert_type in ALL_ALERT_TYPES:
            pref = self._resolve_pref(
                pref_map,
                alert_type=alert_type,
                scope_type=LEGACY_SCOPE_TENANT,
                scope_id=None,
                user_id=user_id,
            )
            tenant_preferences.append(
                {
                    "scope_type": LEGACY_SCOPE_TENANT,
                    "scope_id": None,
                    "alert_type": alert_type,
                    "enabled": pref["enabled"],
                    "email_enabled": pref["email_enabled"],
                    "cooldown_minutes": pref["cooldown_minutes"],
                }
            )

        inbox_preferences = []
        inboxes = db.query(SendingInbox).filter(SendingInbox.tenant_id == tenant_id).order_by(SendingInbox.email_address).all()
        for inbox in inboxes:
            prefs = []
            for alert_type in INBOX_ALERT_TYPES:
                pref = self._resolve_pref(
                    pref_map,
                    alert_type=alert_type,
                    scope_type=LEGACY_SCOPE_INBOX,
                    scope_id=inbox.inbox_id,
                    user_id=user_id,
                )
                prefs.append(
                    {
                        "scope_type": LEGACY_SCOPE_INBOX,
                        "scope_id": inbox.inbox_id,
                        "alert_type": alert_type,
                        "enabled": pref["enabled"],
                        "email_enabled": pref["email_enabled"],
                        "cooldown_minutes": pref["cooldown_minutes"],
                    }
                )
            inbox_preferences.append(
                {
                    "inbox_id": inbox.inbox_id,
                    "email_address": inbox.email_address,
                    "preferences": prefs,
                }
            )

        return {
            "tenant_preferences": tenant_preferences,
            "inbox_preferences": inbox_preferences,
        }

    def upsert_preferences(self, db: Session, tenant_id: str, user_id: str, items: List[Dict[str, Any]]) -> None:
        inbox_ids = {
            inbox_id
            for (inbox_id,) in db.query(SendingInbox.inbox_id).filter(SendingInbox.tenant_id == tenant_id).all()
        }
        for item in items:
            alert_type = (item.get("alert_type") or "").upper()
            scope_type = (item.get("scope_type") or LEGACY_SCOPE_TENANT).upper()
            scope_id = item.get("scope_id")

            if alert_type not in ALL_ALERT_TYPES:
                raise ValueError(f"Unsupported alert_type: {alert_type}")
            if scope_type not in (LEGACY_SCOPE_TENANT, LEGACY_SCOPE_INBOX):
                raise ValueError(f"Unsupported scope_type: {scope_type}")
            if scope_type == LEGACY_SCOPE_INBOX:
                if not scope_id:
                    raise ValueError("scope_id is required for INBOX scope")
                if scope_id not in inbox_ids:
                    raise ValueError("Inbox not found in tenant")
                store_scope_type = PREF_SCOPE_USER_INBOX
                store_scope_id = self._user_inbox_scope_id(user_id, scope_id)
            else:
                store_scope_type = PREF_SCOPE_USER
                store_scope_id = user_id

            existing = (
                db.query(AlertPreference)
                .filter(
                    AlertPreference.tenant_id == tenant_id,
                    AlertPreference.scope_type == store_scope_type,
                    AlertPreference.scope_id == store_scope_id,
                    AlertPreference.alert_type == alert_type,
                )
                .first()
            )

            if not existing:
                existing = AlertPreference(
                    tenant_id=tenant_id,
                    scope_type=store_scope_type,
                    scope_id=store_scope_id,
                    alert_type=alert_type,
                )
                db.add(existing)

            existing.enabled = bool(item.get("enabled", True))
            existing.email_enabled = bool(item.get("email_enabled", False))
            existing.cooldown_minutes = int(item.get("cooldown_minutes") or 360)

        db.commit()

    @staticmethod
    def _frontend_base_url() -> str:
        raw = settings.FRONTEND_URL or ""
        first = next((part.strip() for part in raw.split(",") if part.strip()), "http://localhost:5173")
        return first.rstrip("/")

    async def dispatch_alert_emails_for_tenant(self, db: Session, tenant_id: str) -> Dict[str, int]:
        recipients = (
            db.query(User.user_id, User.email)
            .filter(
                User.tenant_id == tenant_id,
                User.status == "ACTIVE",
                User.role.in_(["SUPER_ADMIN", "ADMIN"]),
            )
            .all()
        )
        recipients = [(uid, email) for (uid, email) in recipients if email]
        if not recipients:
            return {"sent": 0, "skipped": 0, "failed": 0}

        frontend_base = self._frontend_base_url()
        now = datetime.utcnow()
        sent_count = 0
        skipped_count = 0
        failed_count = 0

        for recipient_user_id, recipient_email in recipients:
            alerts = self.collect_dashboard_alerts(db, tenant_id, user_id=recipient_user_id)
            if not alerts:
                continue

            for alert in alerts:
                if not alert.get("email_enabled"):
                    skipped_count += 1
                    continue

                alert_key = alert["id"]
                cooldown_minutes = int(alert.get("cooldown_minutes") or 360)
                cooldown_delta = timedelta(minutes=max(cooldown_minutes, 1))
                route = alert.get("route") or ""
                route_url = f"{frontend_base}{route}" if route.startswith("/") else frontend_base

                subject = f"[Alert Center] {alert.get('severity', 'INFO')} - {alert.get('title', 'Alert')}"
                body = (
                    f"Alert: {alert.get('title', 'Alert')}\n"
                    f"Severity: {alert.get('severity', 'INFO')}\n"
                    f"Source: {alert.get('source', 'SYSTEM')}\n\n"
                    f"{alert.get('message', '')}\n\n"
                    f"Open in app: {route_url}\n"
                )

                dispatch = (
                    db.query(AlertEmailDispatch)
                    .filter(
                        AlertEmailDispatch.tenant_id == tenant_id,
                        AlertEmailDispatch.alert_key == alert_key,
                        AlertEmailDispatch.recipient_email == recipient_email,
                    )
                    .first()
                )

                if dispatch and dispatch.last_sent_at and (now - dispatch.last_sent_at) < cooldown_delta:
                    skipped_count += 1
                    continue

                try:
                    result = await email_sender.send_plain_email(
                        to_email=recipient_email,
                        subject=subject,
                        body_content=body,
                    )
                    if result.get("success"):
                        if not dispatch:
                            dispatch = AlertEmailDispatch(
                                tenant_id=tenant_id,
                                alert_key=alert_key,
                                recipient_email=recipient_email,
                            )
                            db.add(dispatch)
                        dispatch.last_sent_at = now
                        dispatch.send_count = int(dispatch.send_count or 0) + 1
                        sent_count += 1
                    else:
                        failed_count += 1
                        logger.warning(
                            f"[ALERT-EMAIL] Failed recipient={recipient_email} key={alert_key}: {result.get('error')}"
                        )
                except Exception as exc:
                    failed_count += 1
                    logger.error(f"[ALERT-EMAIL] Unexpected failure recipient={recipient_email} key={alert_key}: {exc}")

        db.commit()
        return {"sent": sent_count, "skipped": skipped_count, "failed": failed_count}

    async def evaluate_and_dispatch_all(self, db: Session) -> Dict[str, Any]:
        tenant_ids = set()
        tenant_ids.update(
            t for (t,) in db.query(SendingInbox.tenant_id).filter(SendingInbox.tenant_id.isnot(None)).distinct().all()
        )
        tenant_ids.update(
            t for (t,) in db.query(Campaign.tenant_id).filter(Campaign.tenant_id.isnot(None)).distinct().all()
        )

        summary = {"tenants": 0, "sent": 0, "skipped": 0, "failed": 0}
        for tenant_id in tenant_ids:
            stats = await self.dispatch_alert_emails_for_tenant(db, tenant_id)
            summary["tenants"] += 1
            summary["sent"] += stats["sent"]
            summary["skipped"] += stats["skipped"]
            summary["failed"] += stats["failed"]
        return summary


alert_center_service = AlertCenterService()
