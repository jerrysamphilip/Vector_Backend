"""
Read-only ingestion service for external mailbox-provider reputation signals.
Phase 1: store metrics/events for analytics, no send-behavior side effects.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.provider_reputation import (
    ExternalReputationMetric,
    ExternalFeedbackEvent,
    ExternalIngestionRun,
)


class ProviderIngestionService:
    PROVIDERS = {"GOOGLE_POSTMASTER", "SNDS", "JMRP"}

    def _record_run(
        self,
        db: Session,
        provider: str,
        domain_name: Optional[str],
        source: str,
        status: str,
        records_ingested: int,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        run = ExternalIngestionRun(
            provider=provider,
            domain_name=domain_name,
            source=source,
            status=status,
            records_ingested=records_ingested,
            error_message=error_message,
            completed_at=datetime.utcnow(),
        )
        db.add(run)
        db.commit()
        return {
            "run_id": run.run_id,
            "provider": provider,
            "status": status,
            "records_ingested": records_ingested,
            "error_message": error_message,
        }

    def _fetch_url(self, url: str) -> str:
        req = Request(url, headers={"User-Agent": "SALES_PRO-Ingestion/1.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _enabled(self, provider: str) -> bool:
        if provider == "GOOGLE_POSTMASTER":
            return settings.ENABLE_POSTMASTER_INGEST
        if provider == "SNDS":
            return settings.ENABLE_SNDS_INGEST
        if provider == "JMRP":
            return settings.ENABLE_JMRP_INGEST
        return False

    def _feed_url(self, provider: str) -> str:
        if provider == "GOOGLE_POSTMASTER":
            return settings.POSTMASTER_FEED_URL
        if provider == "SNDS":
            return settings.SNDS_FEED_URL
        if provider == "JMRP":
            return settings.JMRP_FEED_URL
        return ""

    def ingest(
        self,
        db: Session,
        provider: str,
        domain_name: str,
        payload: Optional[Any] = None,
        source: str = "MANUAL",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        provider = (provider or "").upper()
        if provider not in self.PROVIDERS:
            return {"status": "FAILED", "error": "Unsupported provider", "records_ingested": 0}

        if payload is None and source == "FEED_URL":
            if not self._enabled(provider):
                return self._record_run(db, provider, domain_name, source, "SKIPPED", 0, "Provider ingest disabled")
            feed_url = self._feed_url(provider)
            if not feed_url:
                return self._record_run(db, provider, domain_name, source, "SKIPPED", 0, "Feed URL not configured")
            try:
                raw = self._fetch_url(feed_url)
                if provider == "SNDS":
                    payload = raw
                else:
                    payload = json.loads(raw)
            except Exception as exc:
                return self._record_run(db, provider, domain_name, source, "FAILED", 0, f"Feed fetch failed: {exc}")

        try:
            records = 0
            if provider == "GOOGLE_POSTMASTER":
                records = self._ingest_postmaster(db, domain_name, payload, dry_run)
            elif provider == "SNDS":
                records = self._ingest_snds(db, domain_name, payload, dry_run)
            elif provider == "JMRP":
                records = self._ingest_jmrp(db, domain_name, payload, dry_run)

            if dry_run:
                return {
                    "provider": provider,
                    "status": "SUCCESS",
                    "records_ingested": records,
                    "dry_run": True,
                }
            return self._record_run(db, provider, domain_name, source, "SUCCESS", records, None)
        except Exception as exc:
            if dry_run:
                return {"provider": provider, "status": "FAILED", "records_ingested": 0, "error": str(exc), "dry_run": True}
            return self._record_run(db, provider, domain_name, source, "FAILED", 0, str(exc))

    def _ingest_postmaster(self, db: Session, domain_name: str, payload: Any, dry_run: bool) -> int:
        # Expected payload: dict or list of dict with keys like date/spam_rate/reputation_score
        rows = payload if isinstance(payload, list) else [payload or {}]
        count = 0
        for row in rows:
            metric_date = str(row.get("date") or datetime.utcnow().date())
            if not dry_run:
                db.add(ExternalReputationMetric(
                    provider="GOOGLE_POSTMASTER",
                    domain_name=domain_name,
                    metric_date=metric_date,
                    spam_rate=self._to_float(row.get("spam_rate")),
                    complaint_rate=self._to_float(row.get("feedback_loop_rate")),
                    bounce_rate=self._to_float(row.get("bounce_rate")),
                    reputation_score=self._to_float(row.get("reputation_score")),
                    delivery_error_rate=self._to_float(row.get("delivery_errors")),
                    source="MANUAL",
                    raw_payload=row,
                ))
            count += 1
        if not dry_run:
            db.commit()
        return count

    def _ingest_snds(self, db: Session, domain_name: str, payload: Any, dry_run: bool) -> int:
        # Expected payload: CSV string (or list[dict])
        count = 0
        if isinstance(payload, list):
            rows = payload
        else:
            text = payload or ""
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)

        for row in rows:
            metric_date = str(row.get("date") or row.get("Date") or datetime.utcnow().date())
            if not dry_run:
                db.add(ExternalReputationMetric(
                    provider="SNDS",
                    domain_name=domain_name,
                    metric_date=metric_date,
                    spam_rate=self._to_float(row.get("SpamRate")),
                    complaint_rate=self._to_float(row.get("ComplaintRate")),
                    bounce_rate=self._to_float(row.get("BounceRate")),
                    reputation_score=self._to_float(row.get("ReputationScore")),
                    delivery_error_rate=self._to_float(row.get("DeliveryErrorRate")),
                    source="MANUAL",
                    raw_payload=row,
                ))
            count += 1
        if not dry_run:
            db.commit()
        return count

    def _ingest_jmrp(self, db: Session, domain_name: str, payload: Any, dry_run: bool) -> int:
        # Expected payload: list of feedback events
        rows = payload if isinstance(payload, list) else [payload or {}]
        count = 0
        for row in rows:
            event_time = row.get("event_time")
            parsed_time = None
            if isinstance(event_time, str):
                try:
                    parsed_time = datetime.fromisoformat(event_time.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    parsed_time = None
            if not dry_run:
                db.add(ExternalFeedbackEvent(
                    provider="JMRP",
                    domain_name=domain_name,
                    event_type=str(row.get("event_type") or "COMPLAINT"),
                    event_time=parsed_time,
                    recipient_hash=row.get("recipient_hash"),
                    raw_payload=row,
                ))
            count += 1
        if not dry_run:
            db.commit()
        return count

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except Exception:
            return None

    def get_status(self, db: Session) -> Dict[str, Any]:
        latest = {}
        for provider in sorted(self.PROVIDERS):
            row = (
                db.query(ExternalIngestionRun)
                .filter(ExternalIngestionRun.provider == provider)
                .order_by(ExternalIngestionRun.started_at.desc())
                .first()
            )
            latest[provider] = {
                "enabled": self._enabled(provider),
                "feed_url_configured": bool(self._feed_url(provider)),
                "last_run": {
                    "run_id": row.run_id,
                    "status": row.status,
                    "records_ingested": row.records_ingested,
                    "error_message": row.error_message,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                } if row else None
            }
        return latest


provider_ingestion_service = ProviderIngestionService()
