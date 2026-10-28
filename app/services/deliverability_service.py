# app/services/deliverability_service.py
"""
Deliverability and reputation monitoring (BR-DF-04 / 07).

``DeliverabilityService`` (the module singleton ``deliverability_service``) holds the Amazon SES clients
and the jobs' / webhook's ``fn(..., db)`` operations: SES account sync, DNS scans, snapshots, domain
verification and the reputation safety switch. ``SendingDomainService`` serves
app/routers/deliverability_router.py: one method per route, each one unit of work, errors as domain
errors. SQL lives in app/repositories/deliverability.py and the rules in app/domain/deliverability.py.
Sending domains are tenant-owned: every tenant-facing read and write names the caller's tenant.
"""
import logging
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

import boto3
import dns.resolver

from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.domain import deliverability as rules
from app.models.domain_reputation import DomainHealthSnapshot, ReputationAlert
from app.models.email_message import EmailEvent
from app.repositories.deliverability import (DeliverabilityRepository, DeliverabilityUnitOfWork, SendingDomain,
                                             Session, borrow)
from app.services import send_safety
from app.services.alert_center_service import alert_center_service
from app.services.domain_tenancy import get_or_create_tenant_domain, get_tenant_domain, tenant_has_domain
from app.services.provider_ingestion_service import provider_ingestion_service
from app.utils.retry_utils import retry_aws_call

logger = logging.getLogger(__name__)

DOMAIN_NOT_FOUND = "Domain not found"
DOMAIN_EXISTS = "This domain is already in your workspace"
NEW_DOMAIN_DEFAULTS = dict(spf_status="UNKNOWN", dkim_status="UNKNOWN", dmarc_status="UNKNOWN",
                           warmup_status="WARMING", safety_threshold=rules.DEFAULT_SAFETY_THRESHOLD)
INBOX_DOMAIN_DEFAULTS = dict(current_reputation_score=100, safety_threshold=rules.DEFAULT_SAFETY_THRESHOLD,
                             warmup_status="COMPLETED", spf_status="UNKNOWN", dkim_status="UNKNOWN",
                             dmarc_status="UNKNOWN", is_blacklisted=False)
DOMAIN_FIELDS = ("domain_name", "domain_id", "spf_status", "dkim_status", "dmarc_status",
                 "current_reputation_score", "safety_threshold", "is_blacklisted", "warmup_status",
                 "ses_reputation_status", "sesv2_enabled", "account_reputation_score", "created_at", "updated_at")


def _txt_records(name: str) -> List[str]:
    return [rd.to_text().strip('"') for rd in dns.resolver.resolve(name, 'TXT')]


def _spf_has_ses(check_domain: str) -> bool:
    """True if the domain's SPF record authorises amazonses.com (directly or one include deep)."""
    try:
        for txt in _txt_records(check_domain):
            if not txt.startswith("v=spf1"):
                continue
            if "amazonses.com" in txt:
                return True
            if any(_include_has_ses(inc) for inc in rules.SPF_INCLUDE_RE.findall(txt)):
                return True
    except Exception as exc:
        logger.debug("SPF TXT lookup for %s failed: %s", check_domain, exc)
    return False


def _include_has_ses(include: str) -> bool:
    try:
        return any("amazonses.com" in txt for txt in _txt_records(include))
    except Exception as exc:  # DNS lookups fail routinely; treat as "no SES include"
        logger.debug("SPF include lookup for %s failed: %s", include, exc)
        return False


def _dmarc(domain_name: str) -> str:
    try:
        status = rules.dmarc_status(_txt_records(f"_dmarc.{domain_name}"))
    except Exception as e:
        logger.info(f"[Deliverability] DMARC check failed for {domain_name}: {e}")
        return "FAIL"
    if status == "WARNING":
        logger.warning(f"[Deliverability] DMARC exists for {domain_name} but policy is 'none' "
                       f"(no enforcement). Consider p=quarantine or p=reject.")
    return status


LIVE_DNS_TIMEOUT_SECONDS = 2.0
DnsLookup = Callable[[str, str], Optional[List[str]]]


def _rdata_text(rtype: str, rd: Any) -> str:
    if rtype == "TXT":  # a long TXT record is several strings: they form one value
        return b"".join(rd.strings).decode("utf-8", errors="replace")
    if rtype == "MX":
        return f"{rd.preference} {rd.exchange.to_text()}"
    return str(rd.target.to_text()) if rtype == "CNAME" else str(rd.to_text())


def live_dns_values(rtype: str, host: str) -> Optional[List[str]]:
    """What DNS publishes for ``host`` (S-25): [] when the name or record type does not exist, None when the
    lookup itself failed (timeout, no name servers)."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = LIVE_DNS_TIMEOUT_SECONDS
    try:
        return [_rdata_text(rtype, rd) for rd in resolver.resolve(host, rtype)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except Exception as exc:  # DNS failures are reported as UNKNOWN, never as an error
        logger.info("Live DNS lookup %s %s failed: %s", rtype, host, exc)
        return None


def compare_with_live_dns(records: List[Dict[str, Any]], lookup: DnsLookup) -> List[Dict[str, Any]]:
    """Each guidance record with its live status. After one failed lookup (DNS unreachable) the remaining
    records are UNKNOWN without waiting for more timeouts."""
    out, reachable = [], True
    for record in records:
        live = None
        if reachable and "<" not in str(record.get("host", "")):
            live = lookup(str(record["type"]), str(record["host"]))
            reachable = live is not None
        out.append(rules.compare_record(record, live))
    return out


def _domain_dict(d: SendingDomain, inbox_emails: List[str], active_campaigns: List[str]) -> Dict[str, Any]:
    out = {field: getattr(d, field) for field in DOMAIN_FIELDS}
    out.update(associated_inboxes=inbox_emails, active_campaigns=active_campaigns)
    return out


class DeliverabilityService:
    def __init__(self) -> None:
        client_kwargs = {"region_name": settings.AWS_REGION}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY

        self.ses_v2 = boto3.client("sesv2", **client_kwargs)
        self.ses_v1 = boto3.client("ses", **client_kwargs)

    # ── Statistics ────────────────────────────────────────────────────────
    def get_sending_statistics(self) -> dict:
        """
        Sending statistics (Sends, Bounces, Complaints, Rejects) from AWS SES with their summary.
        Account-wide (every tenant's mail): platform admins only.
        """
        try:
            response = retry_aws_call(self.ses_v1.get_send_statistics)()
            return rules.ses_statistics(response.get("SendDataPoints", []))
        except Exception as e:
            logger.error(f"[Deliverability] Failed to fetch stats: {e}")
            return {"data_points": [], "summary": {}}

    def get_domain_statistics(self, db: Session, domain: Optional[str] = None,
                              tenant_id: Optional[str] = None) -> dict:
        """
        The same shape as get_sending_statistics() from the LOCAL database over the last 14 days:
        sends, bounces and complaints (unsubscribe events) per day.
        tenant_id: only that tenant's mail (always set for tenant users).
        domain: only mail sent from that domain; None = all of the tenant's mail.
        """
        start = utcnow() - timedelta(days=rules.STATISTICS_DAYS)
        repo = borrow(db)
        events = repo.daily_events(tenant_id, domain, start, [EmailEvent.EVENT_BOUNCE, EmailEvent.EVENT_UNSUBSCRIBE])
        return rules.local_statistics(
            start, repo.daily_sends(tenant_id, domain, start),
            [(d, n) for d, t, n in events if t == EmailEvent.EVENT_BOUNCE],
            [(d, n) for d, t, n in events if t == EmailEvent.EVENT_UNSUBSCRIBE])

    # ── SES account (job) ─────────────────────────────────────────────────
    def sync_ses_metrics(self, db: Session) -> bool:
        """
        Fetch account-level reputation and SESv2 status from AWS and write it to every tenant's
        domain rows (the SES account's reputation affects all senders in it).
        """
        try:
            response = retry_aws_call(self.ses_v2.get_account)()
            reputation = response.get("Reputation", {})
            # SESv2 reports HEALTHY / PROBATION / SHUTDOWN as EnforcementStatus
            account_status = response.get("EnforcementStatus") or reputation.get("ReputationStatus", "HEALTHY")
            repo = borrow(db)
            if response.get("SendingEnabled") is False or account_status == "SHUTDOWN":
                # Nothing can be sent: pause active campaigns with the reason instead of
                # letting every send fail (BR-DF-07)
                reason = f"Amazon SES sending is disabled for this account (status {account_status})"
                for campaign in repo.active_campaigns():
                    send_safety.auto_pause(db, campaign, reason)
            account_score = reputation.get("AccountReputationScore", 1.0)
            sesv2_enabled = response.get("ProductionAccessEnabled", False)
            logger.info(f"[Deliverability] SESv2 Sync: Status={account_status}, Score={account_score}")
            for domain in repo.owned_domains():
                domain.ses_reputation_status = account_status
                domain.account_reputation_score = account_score
                domain.sesv2_enabled = sesv2_enabled
                domain.updated_at = utcnow()
            db.commit()
            return True
        except Exception as e:
            logger.error(f"[Deliverability] AWS SESv2 sync failed: {e}")
            return False

    # ── Reputation events ─────────────────────────────────────────────────
    def update_domain_stats(self, domain_name: str, event_type: str, db: Session,
                            tenant_id: Optional[str] = None) -> Any:
        """Kept for callers outside the webhook; health is now rate-based (send_safety).
        tenant_id: the tenant whose mail caused the event (domain records are per tenant)."""
        if not tenant_id:
            logger.warning(f"[Deliverability] update_domain_stats({domain_name}) without a tenant; ignored")
            return None
        if event_type in ("BOUNCE", "COMPLAINT"):
            send_safety.check_domain_health(db, domain_name, event_type.lower(), tenant_id=tenant_id)
            return None
        return self._legacy_update_domain_stats(domain_name, event_type, db, tenant_id)

    def _legacy_update_domain_stats(self, domain_name: str, event_type: str, db: Session, tenant_id: str) -> None:
        """Event-based score (bounce -5, complaint -20, open +1); the safety switch below the threshold."""
        domain = get_or_create_tenant_domain(db, tenant_id, domain_name)
        domain.current_reputation_score = rules.legacy_score(domain.current_reputation_score, event_type)
        logger.info(f"[Deliverability] Domain {domain_name} (tenant {tenant_id}) score updated to "
                    f"{domain.current_reputation_score} (Event: {event_type})")
        if domain.current_reputation_score < (domain.safety_threshold or rules.DEFAULT_SAFETY_THRESHOLD):
            self._trigger_safety_switch(domain, db)
        db.commit()

    def _trigger_safety_switch(self, domain: SendingDomain, db: Session) -> None:
        """PAUSE the owning tenant's active campaigns using this domain and alert."""
        repo = borrow(db)
        repo.add(ReputationAlert(
            domain_id=domain.domain_id, tenant_id=domain.tenant_id, domain_name=domain.domain_name,
            alert_type="REPUTATION_DROP", severity="CRITICAL",
            details=f"Score dropped to {domain.current_reputation_score} (Threshold: {domain.safety_threshold}). "
                    f"Campaigns Paused."))
        affected = repo.active_campaigns_on_domain(domain.tenant_id, domain.domain_name, distinct=False)
        # Same pause as the other safety paths: freeze the queue, record why, tell owner + admins
        reason = (f"sending domain {domain.domain_name} reputation score dropped to "
                  f"{domain.current_reputation_score} (threshold "
                  f"{domain.safety_threshold or rules.DEFAULT_SAFETY_THRESHOLD})")
        for campaign in affected:
            send_safety.auto_pause(db, campaign, reason)
        logger.warning(f"[SAFETY SWITCH] Paused {len(affected)} campaign(s) for {domain.domain_name} "
                       f"(tenant {domain.tenant_id}) due to low reputation!")

    # ── Snapshots and domain rows ─────────────────────────────────────────
    def create_snapshot(self, db: Session, tenant_id: Optional[str] = None) -> int:
        """Snapshot domain health. tenant_id: only that tenant's domains; None = every tenant-owned
        domain. Returns how many snapshots were written. Commits."""
        written = self.add_snapshots(borrow(db), borrow(db).owned_domains(tenant_id))
        db.commit()
        return written

    @staticmethod
    def add_snapshots(repo: DeliverabilityRepository, domains: Iterable[SendingDomain]) -> int:
        n = 0
        for d in domains:
            repo.add(DomainHealthSnapshot(domain_id=d.domain_id, tenant_id=d.tenant_id, domain_name=d.domain_name,
                                          **rules.snapshot_values(d.current_reputation_score, d.bounce_rate_24h,
                                                                  d.complaint_rate_24h)))
            n += 1
        return n

    def sync_from_inboxes(self, db: Session, tenant_id: Optional[str] = None) -> int:
        """Ensure every (tenant, inbox domain) pair has a domain record. tenant_id: only that tenant."""
        pairs = {(t, d) for t, email in borrow(db).inbox_addresses(tenant_id)
                 if t and (d := rules.domain_of(email))}
        synced_count = 0
        try:
            for t, d_name in sorted(pairs):
                if get_tenant_domain(db, t, d_name) is None:
                    logger.info(f"[Deliverability] Syncing new domain from inbox: {d_name} (tenant {t})")
                    get_or_create_tenant_domain(db, t, d_name, **INBOX_DOMAIN_DEFAULTS)
                    synced_count += 1
            db.commit()
            return synced_count
        except Exception as e:
            db.rollback()
            logger.error(f"[Deliverability] sync_from_inboxes failed: {e}")
            raise

    # ── DNS scan ──────────────────────────────────────────────────────────
    def _spf(self, domain_name: str) -> str:
        mail_from = f"mail.{domain_name}"
        if _spf_has_ses(mail_from):
            logger.info(f"[Deliverability] SPF PASS for {domain_name} via Custom MAIL FROM "
                        f"subdomain ({mail_from} includes amazonses.com)")
            return "PASS"
        status = rules.spf_status(False, _spf_has_ses(domain_name))
        if status == "WARNING":
            logger.warning(f"[Deliverability] SPF WARNING for {domain_name}: neither {mail_from} nor root "
                           f"domain authorises amazonses.com. Add 'v=spf1 include:amazonses.com ~all' to "
                           f"{mail_from} TXT record.")
        return status

    def _dkim(self, domain_name: str) -> str:
        """DKIM from AWS SES (the source of truth: DNS can't be scanned without the selector)."""
        try:
            response = self.ses_v1.get_identity_dkim_attributes(Identities=[domain_name])
            attrs = response.get("DkimAttributes", {}).get(domain_name, {})
            return rules.dkim_status(attrs.get("DkimVerificationStatus"))
        except Exception as e:
            logger.info(f"[Deliverability] AWS DKIM check failed for {domain_name}: {e}")
            return "UNKNOWN"

    def perform_dns_scan(self, domain_name: str, db: Session, tenant_id: Optional[str] = None) -> dict:
        """
        Real-time DNS queries for SPF (SES Custom MAIL FROM subdomain mail.<domain>, then the root) and
        DMARC, and the DKIM status from AWS SES. DNS and the SES identity are facts about the domain
        itself, so one scan is written to the domain record of the given tenant (created if the tenant
        has none yet), or with tenant_id None (scheduled job) to every tenant's record for the name.
        """
        logger.info(f"[Deliverability] Starting DNS scan for {domain_name}")
        spf, dmarc, dkim = self._spf(domain_name), _dmarc(domain_name), self._dkim(domain_name)
        rows = ([get_or_create_tenant_domain(db, tenant_id, domain_name)] if tenant_id
                else borrow(db).rows_named(domain_name))
        for domain in rows:
            domain.spf_status, domain.dmarc_status, domain.dkim_status = spf, dmarc, dkim
            domain.updated_at = utcnow()
        db.commit()
        return {"domain": domain_name, "spf": spf, "dmarc": dmarc, "dkim": dkim,
                "warnings": rules.scan_warnings(domain_name, spf, dmarc)}

    def scan_all_domains(self, db: Session) -> int:
        """Scheduled: each tenant-owned domain name once (results go to every tenant's row)."""
        names = borrow(db).owned_domain_names()
        db.rollback()  # no read transaction across the DNS lookups
        for name in names:
            self.perform_dns_scan(name, db)
        return len(names)

    # ── Domain list and verification ──────────────────────────────────────
    def get_all_domains_enriched(self, db: Session, tenant_id: str) -> List[Dict[str, Any]]:
        """The tenant's domains with the tenant's mailboxes on each and their active campaigns."""
        repo = borrow(db)
        results = []
        for d in repo.tenant_domains(tenant_id):
            inboxes = repo.inboxes_on_domain(tenant_id, d.domain_name)
            active = {c.campaign_name for inbox in inboxes for c in inbox.campaigns
                      if c.status == "ACTIVE" and c.tenant_id == tenant_id}
            results.append(_domain_dict(d, [i.email_address for i in inboxes], list(active)))
        return results

    def verify_domain_and_get_tokens(self, domain_name: str, db: Session, tenant_id: Optional[str] = None) -> dict:
        """
        Initiate DKIM verification for a domain and return the DNS records needed; the tenant gets
        its own SendingDomain record for it (even when the AWS call fails).

        The SES domain identity is account-level in AWS (one per domain for the whole account), so
        several tenants on the same domain share it: verify_domain_dkim is idempotent and returns the
        same tokens again. Status is stored per tenant.
        """
        domain_name = (domain_name or "").strip().lower()
        logger.info(f"[Deliverability] Verifying domain identity: {domain_name} (tenant {tenant_id})")
        domain = None
        if tenant_id:
            domain = get_tenant_domain(db, tenant_id, domain_name)
            if domain is None:
                domain = get_or_create_tenant_domain(db, tenant_id, domain_name, **NEW_DOMAIN_DEFAULTS)
                db.commit()
        try:
            dkim_tokens = self.ses_v1.verify_domain_dkim(Domain=domain_name).get("DkimTokens", [])
        except Exception as e:
            logger.error(f"[Deliverability] Failed to verify domain {domain_name}: {e}")
            raise
        # We just asked for tokens: DKIM is pending until DNS is in place
        if domain is not None and domain.dkim_status != "PASS":
            domain.dkim_status = "PENDING"
            db.commit()
        return {"domain": domain_name, "dkim_tokens": dkim_tokens,
                "dns_records": rules.verification_records(domain_name, dkim_tokens),
                "verification_status": "PENDING"}

    def dkim_tokens(self, domain_name: str, create: bool) -> List[str]:
        """The SES DKIM tokens for the domain ([] when SES is unreachable or has none).
        create=True asks SES to start DKIM verification (idempotent, account-level identity)."""
        try:
            if create:
                tokens = self.ses_v1.verify_domain_dkim(Domain=domain_name).get("DkimTokens", [])
            else:
                attrs = self.ses_v1.get_identity_dkim_attributes(Identities=[domain_name])
                tokens = attrs.get("DkimAttributes", {}).get(domain_name, {}).get("DkimTokens", [])
            return [str(t)[:100] for t in (tokens or [])][:10]
        except Exception as exc:  # no AWS credentials / network: guidance without tokens
            logger.info("[Deliverability] SES DKIM tokens unavailable for %s: %s", domain_name, type(exc).__name__)
            return []


deliverability_service = DeliverabilityService()


class DomainCreateRejected(BadRequestError):
    """A domain name that is not a bare hostname, or a workspace at its domain limit (400)."""


class SendingDomainService:
    """The deliverability routes (tenant-scoped). ``engine`` is the SES / DNS side (the singleton)."""

    def __init__(self, uow: Any, *, engine: Optional[DeliverabilityService] = None,
                 ingestion: Any = None, alerts: Any = None,
                 has_domain: Optional[Callable[[Any, str, str], bool]] = None,
                 dns_lookup: Optional[DnsLookup] = None) -> None:
        self.uow = uow
        self._dns_lookup = dns_lookup if dns_lookup is not None else live_dns_values
        self._engine = engine
        self._ingestion = ingestion if ingestion is not None else provider_ingestion_service
        self._alerts = alerts if alerts is not None else alert_center_service
        self._has_domain = has_domain if has_domain is not None else tenant_has_domain

    @classmethod
    def for_session(cls, db: Session) -> "SendingDomainService":
        return cls(DeliverabilityUnitOfWork.for_session(db))

    @property
    def engine(self) -> DeliverabilityService:
        return self._engine if self._engine is not None else deliverability_service

    def _require_domain(self, uow: Any, tenant_id: str, domain_name: Optional[str]) -> str:
        """404 unless the tenant uses this domain. Returns the normalised name."""
        domain = (domain_name or "").strip().lower()
        if not domain or not self._has_domain(uow.session, tenant_id, domain):
            raise NotFoundError(DOMAIN_NOT_FOUND)
        return domain

    # ── Reads ─────────────────────────────────────────────────────────────
    def statistics(self, user: Any, domain: Optional[str]) -> dict:
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain) if domain else None
            return self.engine.get_domain_statistics(uow.session, domain=name, tenant_id=user.tenant_id)

    def account_statistics(self) -> dict:
        return self.engine.get_sending_statistics()

    def domains(self, user: Any) -> List[Dict[str, Any]]:
        with self.uow as uow:
            return self.engine.get_all_domains_enriched(uow.session, user.tenant_id)

    def reputation_alerts(self, user: Any, resolved: bool) -> List[ReputationAlert]:
        with self.uow as uow:
            return uow.domains.reputation_alerts(user.tenant_id, include_resolved=resolved)

    def dashboard_alerts(self, user: Any) -> List[Dict[str, Any]]:
        with self.uow as uow:
            return self._alerts.collect_dashboard_alerts(uow.session, user.tenant_id, user_id=user.user_id)

    def alert_preferences(self, user: Any) -> Dict[str, Any]:
        with self.uow as uow:
            return self._alerts.list_preferences(uow.session, user.tenant_id, user.user_id)

    def save_alert_preferences(self, user: Any, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self.uow as uow:
            try:
                self._alerts.upsert_preferences(uow.session, user.tenant_id, user.user_id, items)
            except ValueError as exc:
                raise BadRequestError(str(exc)) from exc
        return {"status": "updated", "updated": len(items)}

    def dns_records(self, user: Any, domain_name: str, live: bool = True) -> Dict[str, Any]:
        """DNS records to publish for this workspace domain and its last statuses; with ``live`` each record
        is compared with what DNS publishes now (S-25: OK / ADD / UPDATE / UNKNOWN, current value, the
        value to publish - for SPF merged into the existing record). The lookups run after the unit of work."""
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain_name)
            tokens = self.engine.dkim_tokens(name, create=False)
            row = uow.domains.get_domain(user.tenant_id, name)
            records: List[Dict[str, Any]] = rules.dns_record_guidance(name, tokens, settings.AWS_REGION)
            result: Dict[str, Any] = {
                "domain_name": name,
                "dns_records": records,
                "dkim_tokens_available": bool(tokens),
                "spf_status": row.spf_status if row else "UNKNOWN",
                "dkim_status": row.dkim_status if row else "UNKNOWN",
                "dmarc_status": row.dmarc_status if row else "UNKNOWN",
                "checked_at": row.updated_at if row else None,
            }
        if live:
            records = compare_with_live_dns(records, self._dns_lookup)
            result.update(dns_records=records, live_summary=rules.record_summary(records))
            result["live_checked_at"] = utcnow()
        return result

    def sent_log(self, user: Any, domain_name: str, limit: int) -> List[Dict[str, Any]]:
        """Recent outbound emails sent from this domain with their send times (did / when a send happen)."""
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain_name)
            rows = uow.domains.sent_log(user.tenant_id, name, max(1, min(limit, 500)))
            return [{"message_id": m.message_id, "subject": m.subject, "to_email": m.to_email,
                     "from_email": m.from_email, "sent_at": m.sent_at, "scheduled_at": m.scheduled_at,
                     "status": m.status, "campaign_name": campaign_name} for m, campaign_name in rows]

    def integration_status(self, user: Any) -> Any:
        with self.uow as uow:
            return self._ingestion.get_status(uow.session, tenant_id=user.tenant_id)

    def external_metrics(self, user: Any, provider: str, domain_name: str, limit: int) -> List[Any]:
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain_name)
            return uow.domains.external_metrics(user.tenant_id, provider.upper(), name, limit)

    def external_events(self, user: Any, provider: str, domain_name: str, limit: int) -> List[Any]:
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain_name)
            return uow.domains.external_events(user.tenant_id, provider.upper(), name, limit)

    # ── Writes ────────────────────────────────────────────────────────────
    def scan(self, user: Any, domain_name: str) -> Dict[str, Any]:
        """A manual compliance scan (SPF / DKIM / DMARC) of the tenant's domain."""
        with self.uow as uow:
            name = self._require_domain(uow, user.tenant_id, domain_name)
            result = self.engine.perform_dns_scan(name, uow.session, tenant_id=user.tenant_id)
        return {"status": "scan_completed", "results": result}

    def snapshot(self, user: Any) -> Dict[str, str]:
        with self.uow as uow:
            self.engine.create_snapshot(uow.session, tenant_id=user.tenant_id)
        return {"status": "snapshot_created"}

    def delete(self, user: Any, domain_name: str) -> None:
        """The tenant's record of a domain and the tenant's stats for it. Other tenants' records for the
        same name are untouched; the SES identity (account-level) is never deleted here."""
        with self.uow as uow:
            domain = uow.domains.get_domain(user.tenant_id, domain_name)
            if domain is None:
                raise NotFoundError(DOMAIN_NOT_FOUND)
            uow.domains.delete_domain(domain)
            uow.commit()

    def add(self, user: Any, raw_name: str) -> Dict[str, Any]:
        """
        Add a sending domain to the workspace: a valid hostname, not already in the workspace (another
        workspace may add the same name), at most MAX_TENANT_DOMAINS. Starts SES DKIM verification when
        SES is reachable and returns the DNS records to publish. Nothing is verified here.
        """
        name = rules.normalize_domain_name(raw_name)
        if not name:
            raise DomainCreateRejected("Enter a domain name like example.com (no http://, @ or path)")
        with self.uow as uow:
            repo = uow.domains
            if repo.get_domain(user.tenant_id, name):
                raise ConflictError(DOMAIN_EXISTS)
            if repo.count_domains(user.tenant_id) >= rules.MAX_TENANT_DOMAINS:
                raise DomainCreateRejected(
                    f"A workspace can have at most {rules.MAX_TENANT_DOMAINS} sending domains")
            if not repo.insert_domain(SendingDomain(tenant_id=user.tenant_id, domain_name=name, **NEW_DOMAIN_DEFAULTS)):
                raise ConflictError(DOMAIN_EXISTS)
            uow.commit()
            tokens = self.engine.dkim_tokens(name, create=True)
            row = repo.get_domain(user.tenant_id, name)
            if tokens and row is not None and row.dkim_status != "PASS":
                row.dkim_status = "PENDING"
                uow.commit()
            payload = next((d for d in self.engine.get_all_domains_enriched(uow.session, user.tenant_id)
                            if d["domain_name"] == name), None)
        return {"domain": payload, "dns_records": rules.dns_record_guidance(name, tokens, settings.AWS_REGION),
                "dkim_tokens_available": bool(tokens)}

    def ingest(self, user: Any, provider: str, domain_name: Optional[str], payload: Any, source: Any,
               dry_run: Any) -> Dict[str, Any]:
        """
        Read-only ingestion (Google Postmaster / SNDS / JMRP): stores metrics / events only. Stored for
        the caller's tenant; a platform admin ingests for every tenant that has a record of the domain.
        """
        with self.uow as uow:
            if user.role == "PLATFORM_ADMIN":
                name = (domain_name or "").strip().lower()
                tenant_ids = uow.domains.tenants_with_domain(name)
                if not tenant_ids:
                    raise NotFoundError(DOMAIN_NOT_FOUND)
            else:
                name = self._require_domain(uow, user.tenant_id, domain_name or "")
                tenant_ids = [user.tenant_id]
            result: Dict[str, Any] = {}
            for tenant_id in tenant_ids:
                result = self._ingestion.ingest(db=uow.session, provider=provider, domain_name=name,
                                                payload=payload, source=source, dry_run=dry_run, tenant_id=tenant_id)
                if result.get("status") == "FAILED":
                    raise BadRequestError(result.get("error", "Ingestion failed"))
        if len(tenant_ids) > 1:
            result = {**result, "tenants": len(tenant_ids)}
        return result
