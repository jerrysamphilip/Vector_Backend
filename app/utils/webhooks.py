"""
Outbound webhooks (BRD v2.0 BR-SF-20): the pure parts, kept free of the database and of
network side effects so they can be unit tested.

  - EVENT_TYPES            the events a workspace can subscribe an endpoint to
  - sign / verify          X-Outreach-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>
  - next_delay             the retry (exponential backoff) schedule
  - build_body             the JSON envelope, capped at MAX_PAYLOAD_BYTES
  - validate_url           URL + SSRF checks (scheme, credentials, hostnames, resolved IPs)

Receivers verify a delivery by recomputing the HMAC over the timestamp, a ".", and the raw
request body with their endpoint secret, comparing in constant time, and rejecting
timestamps older than a few minutes (replay protection).
"""
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

# ── Event catalogue ────────────────────────────────────────────

# type -> (label, description). Only events the system really emits (see app/services/webhooks.py).
EVENT_TYPES: Dict[str, Tuple[str, str]] = {
    "contact.created": ("Contact created", "A contact was added in the app or through the API."),
    "contact.updated": ("Contact updated", "Fields of a contact changed (imports excluded)."),
    "contact.deleted": ("Contact deleted", "A contact was moved to Recently deleted."),
    "lead.created": ("Lead created", "A lead was opened for a contact."),
    "lead.qualified": ("Lead qualified", "A lead reached the SQL stage."),
    "deal.created": ("Deal created", "An opportunity was created or converted from a lead."),
    "deal.stage_changed": ("Deal stage changed", "An opportunity moved to another stage."),
    "deal.won": ("Deal won", "An opportunity moved to a won stage."),
    "deal.lost": ("Deal lost", "An opportunity moved to a lost stage."),
    "email.replied": ("Email replied", "A contact replied to a campaign email (out-of-office excluded)."),
    "email.bounced": ("Email bounced", "An address hard-bounced and was suppressed."),
    "email.unsubscribed": ("Email unsubscribed", "A contact unsubscribed or complained."),
    "campaign.paused": ("Campaign paused", "A campaign was paused, by a user or automatically."),
}
TEST_EVENT = "webhook.test"

SIGNATURE_HEADER = "X-Outreach-Signature"
SIGNATURE_TOLERANCE_SECONDS = 300

MAX_ATTEMPTS = 6
# Delay before attempt n+1 after attempt n failed (n = 1..5); attempt 6 is the last
BACKOFF_SECONDS = (60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600)
REQUEST_TIMEOUT_SECONDS = 10
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_URL_LENGTH = 2000
DISABLE_AFTER_FAILURES = 20  # consecutive failed attempts before an endpoint is switched off
MAX_ENDPOINTS_PER_TENANT = 10
SECRET_PREFIX = "whsec_"


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def secret_hint(secret: Optional[str]) -> str:
    return f"{SECRET_PREFIX}…{secret[-4:]}" if secret and len(secret) > 8 else ""


def clean_event_types(values: Iterable[str]) -> List[str]:
    """Known event types, de-duplicated, in catalogue order. Raises ValueError on an unknown one."""
    wanted = list(dict.fromkeys(values or []))
    unknown = [v for v in wanted if v not in EVENT_TYPES]
    if unknown:
        raise ValueError(f"Unknown event type(s): {', '.join(str(u)[:50] for u in unknown[:5])}")
    return [t for t in EVENT_TYPES if t in wanted]


# ── Signature ──────────────────────────────────────────────────

def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = str(int(timestamp)).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: Optional[int] = None) -> str:
    """Value of the X-Outreach-Signature header."""
    ts = int(time.time() if timestamp is None else timestamp)
    return f"t={ts},v1={compute_signature(secret, ts, body)}"


def parse_signature(header: str) -> Tuple[Optional[int], List[str]]:
    ts, sigs = None, []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            ts = int(value)
        elif key == "v1" and value:
            sigs.append(value)
    return ts, sigs


def verify(secret: str, header: str, body: bytes, tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
           now: Optional[float] = None) -> bool:
    """True when the header carries a valid v1 signature for body made within `tolerance` seconds."""
    ts, sigs = parse_signature(header)
    if ts is None or not sigs:
        return False
    current = time.time() if now is None else now
    if tolerance and abs(current - ts) > tolerance:
        return False
    expected = compute_signature(secret, ts, body)
    return any(hmac.compare_digest(expected, s) for s in sigs)


# ── Retries ────────────────────────────────────────────────────

def next_delay(attempts: int) -> Optional[int]:
    """Seconds to wait after `attempts` failed attempts; None when no attempt is left."""
    if attempts < 1 or attempts >= MAX_ATTEMPTS:
        return None
    return BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]


def is_success(status_code: Optional[int]) -> bool:
    # Redirects are not followed, so a 3xx is a failure like any other non-2xx
    return status_code is not None and 200 <= status_code < 300


def should_disable(consecutive_failures: int, threshold: int = DISABLE_AFTER_FAILURES) -> bool:
    return consecutive_failures >= threshold


# ── Payload ────────────────────────────────────────────────────

def _default(value) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat() + ("Z" if value.tzinfo is None else "")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)  # Decimal, UUID, ...


def build_body(event_id: str, event_type: str, created_at: datetime, workspace_id: str, data: dict,
               max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes:
    """The JSON envelope. Over max_bytes, `data` is reduced to its *_id fields plus truncated: true."""
    envelope = {"id": event_id, "type": event_type, "created_at": created_at, "workspace_id": workspace_id,
                "data": data or {}}
    body = json.dumps(envelope, default=_default, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) <= max_bytes:
        return body
    slim = {k: v for k, v in (data or {}).items() if k.endswith("_id") and isinstance(v, (str, int))}
    slim["truncated"] = True
    envelope["data"] = slim
    body = json.dumps(envelope, default=_default, ensure_ascii=False, separators=(",", ":")).encode()
    return body[:max_bytes] if len(body) > max_bytes else body


# ── URL / SSRF validation ──────────────────────────────────────

class WebhookUrlError(ValueError):
    pass


# Cloud metadata and host-agent addresses: never a valid target, whatever the environment
METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure / OpenStack metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    ipaddress.ip_address("168.63.129.16"),     # Azure wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata over IPv6
}
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
BLOCKED_HOSTNAMES = {"metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                     "instance-data.ec2.internal"}


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str          # hostname as written (lower case), used for Host / TLS SNI
    port: int
    path: str          # path + query, never empty
    ip: Optional[str] = None  # the validated address to connect to (pinned: no second DNS lookup)


Resolver = Callable[[str, int], List[str]]


def socket_resolver(host: str, port: int) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise WebhookUrlError(f"Could not resolve {host}") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def ip_block_reason(address: str, allow_private: bool = False) -> Optional[str]:
    """Why this address may not be called, or None. allow_private (development only) lets private
    and loopback ranges through; link-local, metadata, multicast and unspecified are always blocked."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "is not a valid IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in METADATA_IPS:
        return "is a cloud metadata address"
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip in _THIS_NETWORK:
        return "is not a routable address"
    if allow_private and (ip.is_private or ip.is_loopback):
        return None
    if ip.is_reserved:
        return "is not a routable address"
    if not ip.is_global:
        return "is a private, loopback or internal address"
    return None


def parse_url(url: str, allow_insecure: bool = False) -> Target:
    """Syntax checks only (no DNS)."""
    if not isinstance(url, str) or not url.strip():
        raise WebhookUrlError("Enter the endpoint URL")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise WebhookUrlError(f"The URL is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise WebhookUrlError("The URL contains spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise WebhookUrlError("The URL is not valid") from exc
    scheme = (parts.scheme or "").lower()
    allowed = ("https", "http") if allow_insecure else ("https",)
    if scheme not in allowed:
        raise WebhookUrlError("The URL must start with https://" if not allow_insecure
                              else "The URL must start with https:// or http://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise WebhookUrlError("The URL must not contain a user name or password")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise WebhookUrlError("The URL has no host name")
    if port == 0:
        raise WebhookUrlError("The URL port is not valid")
    if host in BLOCKED_HOSTNAMES:
        raise WebhookUrlError(f"{host} is not allowed")
    if not allow_insecure and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise WebhookUrlError(f"{host} is not a public host name")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(scheme=scheme, host=host, port=port or (443 if scheme == "https" else 80), path=path)


def validate_url(url: str, allow_insecure: bool = False, resolver: Optional[Resolver] = socket_resolver) -> Target:
    """
    Parse and check a webhook URL. allow_insecure (development / test only) accepts http:// and
    private / loopback addresses. With a resolver, every address the host resolves to must pass
    (a host that also resolves to an internal address is refused); the returned Target carries the
    first one, which the sender connects to without resolving again (no DNS rebinding).
    """
    target = parse_url(url, allow_insecure)
    try:
        literal = ipaddress.ip_address(target.host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    elif resolver is None:
        return target
    else:
        addresses = resolver(target.host, target.port)
        if not addresses:
            raise WebhookUrlError(f"Could not resolve {target.host}")
    for address in addresses:
        reason = ip_block_reason(address, allow_private=allow_insecure)
        if reason:
            raise WebhookUrlError(f"{target.host} {reason}" if literal is None else f"{address} {reason}")
    return Target(target.scheme, target.host, target.port, target.path, addresses[0])
