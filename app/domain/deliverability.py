"""
Deliverability rules (BR-DF-04 / 07): sending-domain names, the DNS records to publish for Amazon SES,
the DNS scan verdicts, the local sending statistics and the auto-pause risk rule. Pure: no I/O; the
thresholds come in as arguments (app/core/config.py settings in production).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_TENANT_DOMAINS = 200  # sending domains one workspace can add
STATISTICS_DAYS = 14      # local statistics window (matches SES's two weeks)
SNAPSHOT_RETENTION_DAYS = 90
SNAPSHOT_INTERVAL = timedelta(minutes=55)  # an hourly job never snapshots a domain twice in an hour
DEFAULT_SAFETY_THRESHOLD = 70


def domain_of(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


# ── Adding a sending domain (validation and DNS guidance) ─────────────────

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def normalize_domain_name(raw: Optional[str]) -> Optional[str]:
    """
    A bare hostname like "mail.example.com" in lower case, or None when it is not one.
    Rejects schemes, paths, ports, e-mail addresses, IP addresses, wildcards, single labels
    and names over 253 characters; a trailing dot is dropped.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or len(name) > 253:
        return None
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        return None
    if not _TLD_RE.match(labels[-1]):  # also rules out IPv4 addresses (numeric last label)
        return None
    return name


def dns_record_guidance(domain_name: str, dkim_tokens: Optional[list] = None, region: Optional[str] = None) -> list:
    """
    The DNS records to publish for sending through Amazon SES from this domain (shown to
    the user; nothing is checked here). DKIM needs the SES tokens; without them the DKIM
    entry says where they come from. Matches what perform_dns_scan checks: SPF on the
    Custom MAIL FROM subdomain mail.<domain> (or the root), DMARC on _dmarc.<domain>.
    """
    records = []
    for token in (dkim_tokens or [])[:10]:
        records.append({"kind": "DKIM", "type": "CNAME", "host": f"{token}._domainkey.{domain_name}",
                        "value": f"{token}.dkim.amazonses.com", "required": True,
                        "note": "Signs your mail so receivers can verify it came from you."})
    if not dkim_tokens:
        records.append({"kind": "DKIM", "type": "CNAME", "host": f"<token>._domainkey.{domain_name}",
                        "value": "<token>.dkim.amazonses.com", "required": True,
                        "note": "Three CNAME records; the tokens are issued by Amazon SES when the domain "
                                "identity is created. Ask your administrator if they are not listed."})
    records.append({"kind": "SPF", "type": "TXT", "host": f"mail.{domain_name}",
                    "value": "v=spf1 include:amazonses.com ~all", "required": True,
                    "note": "On the MAIL FROM subdomain (or merge include:amazonses.com into the SPF "
                            "record on the root domain). Only one SPF record per host."})
    records.append({"kind": "MAIL_FROM", "type": "MX", "host": f"mail.{domain_name}",
                    "value": f"10 feedback-smtp.{region or '<region>'}.amazonses.com", "required": False,
                    "note": "Lets SES use mail.<your domain> as the MAIL FROM (envelope sender)."})
    records.append({"kind": "DMARC", "type": "TXT", "host": f"_dmarc.{domain_name}",
                    "value": f"v=DMARC1; p=none; rua=mailto:dmarc@{domain_name}", "required": True,
                    "note": "Start with p=none to monitor, then move to p=quarantine or p=reject."})
    return records


# ── Auto-pause risk (BR-DF-07) ────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RiskLimits:
    """The auto-pause limits (settings.AUTO_PAUSE_*)."""
    min_sends: int
    bounce_rate: float
    complaint_rate: float
    min_bounces: int
    min_complaints: int


def rates(sends: int, bounces: int, complaints: int) -> Dict[str, Any]:
    return {
        "sends": sends, "bounces": bounces, "complaints": complaints,
        "bounce_rate": bounces / sends if sends else 0.0,
        "complaint_rate": complaints / sends if sends else 0.0,
    }


def risk_reason(r: Dict[str, Any], limits: RiskLimits) -> Optional[str]:
    """Why these numbers are unsafe, or None: rates once there are enough sends, counts before."""
    if r["sends"] >= limits.min_sends:
        if r["bounce_rate"] >= limits.bounce_rate:
            return (f"hard-bounce rate {r['bounce_rate']:.1%} ({r['bounces']} of {r['sends']} sends) "
                    f"is at or above the {limits.bounce_rate:.1%} limit")
        if r["complaint_rate"] >= limits.complaint_rate:
            return (f"spam-complaint rate {r['complaint_rate']:.2%} ({r['complaints']} of {r['sends']} sends) "
                    f"is at or above the {limits.complaint_rate:.2%} limit")
        return None
    if r["bounces"] >= limits.min_bounces:
        return f"{r['bounces']} hard bounces in the first {r['sends']} sends"
    if r["complaints"] >= limits.min_complaints:
        return f"{r['complaints']} spam complaints in the first {r['sends']} sends"
    return None


def health_score(r: Dict[str, Any], limits: RiskLimits) -> int:
    """0-100 from the current rates (recovers as they fall): each rate at its limit costs 40 points."""
    penalty = min(100.0, r["bounce_rate"] / max(limits.bounce_rate, 1e-6) * 40
                  + r["complaint_rate"] / max(limits.complaint_rate, 1e-6) * 40)
    return int(round(100 - penalty))


def legacy_score(current: Optional[int], event_type: str) -> int:
    """Event-based score of the old path: bounce -5, complaint -20, open +1, clamped to 0-100."""
    score = current if current is not None else 100
    score += {"BOUNCE": -5, "COMPLAINT": -20, "OPEN": 1}.get(event_type, 0)
    return max(0, min(100, score))


# ── Sending statistics ────────────────────────────────────────────────────────
def _summary(attempts: int, bounces: int, complaints: int, rejects: int) -> Dict[str, Any]:
    return {
        "total_attempts": attempts, "total_bounces": bounces, "total_complaints": complaints,
        "total_rejects": rejects,
        "bounce_rate": round((bounces / attempts if attempts > 0 else 0.0) * 100, 2),
        "complaint_rate": round((complaints / attempts if attempts > 0 else 0.0) * 100, 2),
    }


def ses_statistics(data_points: List[Dict[str, Any]]) -> Dict[str, Any]:
    """SES GetSendStatistics points, oldest first, with their totals and rates (percent)."""
    points = sorted(data_points, key=lambda x: x["Timestamp"])
    return {"data_points": points, "summary": _summary(
        sum(p["DeliveryAttempts"] for p in points), sum(p["Bounces"] for p in points),
        sum(p["Complaints"] for p in points), sum(p["Rejects"] for p in points))}


def local_statistics(start: datetime, sends: Iterable[Tuple[Optional[date], int]],
                     bounces: Iterable[Tuple[Optional[date], int]],
                     complaints: Iterable[Tuple[Optional[date], int]], days: int = STATISTICS_DAYS) -> Dict[str, Any]:
    """Same shape as ``ses_statistics`` from local counts per day: every day from ``start`` for
    ``days`` + 1 days is present (zeros), plus any other day that has counts."""
    stats: Dict[str, Dict[str, int]] = {}

    def day(key: str) -> Dict[str, int]:
        return stats.setdefault(key, {"DeliveryAttempts": 0, "Bounces": 0, "Complaints": 0, "Rejects": 0})

    for i in range(days + 1):
        day((start + timedelta(days=i)).date().isoformat())
    for field, rows in (("DeliveryAttempts", sends), ("Bounces", bounces), ("Complaints", complaints)):
        for d, n in rows:
            if d:
                day(d.isoformat())[field] += n
    points = [{"Timestamp": datetime.fromisoformat(k), **stats[k]} for k in sorted(stats)]
    return {"data_points": points, "summary": _summary(
        sum(v["DeliveryAttempts"] for v in stats.values()), sum(v["Bounces"] for v in stats.values()),
        sum(v["Complaints"] for v in stats.values()), 0)}


# ── DNS scan verdicts ─────────────────────────────────────────────────────────
SPF_INCLUDE_RE = re.compile(r"include:(\S+)")


def spf_status(mail_from_has_ses: bool, root_has_ses: bool) -> str:
    """SES uses the Custom MAIL FROM subdomain (mail.<domain>) as envelope sender, so SPF passes there
    or on the root; WARNING when neither authorises amazonses.com."""
    return "PASS" if mail_from_has_ses or root_has_ses else "WARNING"


def dmarc_status(txt_records: Iterable[str]) -> str:
    """PASS for an enforcing DMARC record, WARNING for p=none (no enforcement), FAIL without one."""
    for txt in txt_records:
        if txt.startswith("v=DMARC1"):
            return "WARNING" if "p=none" in txt else "PASS"
    return "FAIL"


def dkim_status(verification_status: Optional[str]) -> str:
    """The SES DKIM verification status as a scan verdict."""
    return {"Success": "PASS", "Pending": "WARNING", "Failed": "FAIL"}.get(verification_status or "", "UNKNOWN")


def scan_warnings(domain_name: str, spf: str, dmarc: str) -> List[str]:
    """Actionable warnings for the UI."""
    warnings = []
    if spf == "WARNING":
        warnings.append(
            f"SPF is not configured for SES. "
            f"Create a TXT record on mail.{domain_name} with value: "
            f"'v=spf1 include:amazonses.com ~all' (SES Custom MAIL FROM subdomain)."
        )
    if dmarc == "WARNING":
        warnings.append(
            "DMARC policy is set to 'none' (no enforcement). "
            "Upgrade to p=quarantine or p=reject for better deliverability."
        )
    return warnings


def verification_records(domain_name: str, dkim_tokens: Iterable[str]) -> List[Dict[str, str]]:
    """The DNS records returned when DKIM verification starts (inbox creation)."""
    records = [{"type": "CNAME", "host": f"{token}._domainkey.{domain_name}",
                "value": f"{token}.dkim.amazonses.com", "status": "Verify in DNS"} for token in dkim_tokens]
    records.append({"type": "TXT", "host": "@" if "." in domain_name else domain_name,
                    "value": "v=spf1 include:amazonses.com ~all", "status": "Recommended"})
    records.append({"type": "TXT", "host": f"_dmarc.{domain_name}", "value": "v=DMARC1; p=none;",
                    "status": "Recommended"})
    return records


def snapshot_values(current_reputation_score: Optional[int], bounce_rate_24h: Optional[float],
                    complaint_rate_24h: Optional[float]) -> Dict[str, Any]:
    """The metrics a health snapshot records for a domain."""
    return {"reputation_score": current_reputation_score if current_reputation_score is not None else 100,
            "bounce_rate_24h": bounce_rate_24h or 0.0, "complaint_rate_24h": complaint_rate_24h or 0.0,
            "open_rate_24h": 0.0}
