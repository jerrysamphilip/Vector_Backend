"""
Outbound host checks (SSRF): which addresses the API may connect to on a user's behalf.

Shared by webhook endpoints (app/utils/webhooks.py) and mailbox SMTP / IMAP servers
(app/routers/inbox_router.py, F007). Pure apart from the DNS lookup, which callers can replace.

- ``ip_block_reason``  why one IP address may not be called (metadata, link-local, private, ...)
- ``check_host``       a host name or IP literal: syntax, blocked names, then every resolved address

Cloud metadata, link-local, multicast and unspecified addresses are refused everywhere. Private and
loopback ranges are allowed only with ``allow_private`` (local development: mailpit, test servers).
"""

import ipaddress
import socket
from typing import Callable, List, Optional

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
_MAX_HOST_LENGTH = 253

Resolver = Callable[[str, int], List[str]]


class UnsafeHostError(ValueError):
    """The host may not be connected to; the message says why (safe to show the user)."""


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


def resolve(host: str, port: int) -> List[str]:
    """Every address ``host`` resolves to (deduplicated, in resolver order)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeHostError(f"Could not resolve {host}") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def normalize_host(host: str) -> str:
    """The host name in lower case without a trailing dot; UnsafeHostError when it is not a bare host."""
    if not isinstance(host, str) or not host.strip():
        raise UnsafeHostError("Enter the server host name")
    value = host.strip()
    if len(value) > _MAX_HOST_LENGTH:
        raise UnsafeHostError("The host name is too long")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "/\\@?#" for ch in value):
        raise UnsafeHostError("Enter only the server host name (no scheme, path, user name or spaces)")
    return value.lower().rstrip(".").strip("[]")


def check_host(host: str, port: int = 0, allow_private: bool = False,
               resolver: Optional[Resolver] = resolve) -> List[str]:
    """Raise UnsafeHostError unless ``host`` (and every address it resolves to) may be connected to.

    An IP literal is checked as it is. A name is checked against BLOCKED_HOSTNAMES, then resolved
    with ``resolver`` (None skips DNS: only the literal / name checks run) and every address must
    pass: a name that also resolves to an internal address is refused. Returns the addresses checked.
    """
    name = normalize_host(host)
    if port and not 0 < port < 65536:
        raise UnsafeHostError("The port must be between 1 and 65535")
    if name in BLOCKED_HOSTNAMES:
        raise UnsafeHostError(f"{name} is not allowed")
    try:
        literal: Optional[str] = str(ipaddress.ip_address(name.split("%", 1)[0]))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    elif resolver is None:
        return []
    else:
        addresses = resolver(name, port or 0)
        if not addresses:
            raise UnsafeHostError(f"Could not resolve {name}")
    for address in addresses:
        reason = ip_block_reason(address, allow_private=allow_private)
        if reason:
            raise UnsafeHostError(f"{name} {reason}" if literal is None else f"{address} {reason}")
    return addresses
