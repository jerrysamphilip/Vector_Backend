# app/core/totp.py
"""
RFC 6238 time-based one-time passwords (TOTP) for two-factor sign-in, without extra dependencies.

30-second steps, 6 digits, HMAC-SHA1 (what Google Authenticator, Microsoft Authenticator, 1Password
and Authy expect by default). verify() accepts the current step and one step either side to allow
for clock drift, and refuses any step at or before the last one the user already used, so a code
cannot be replayed within its window.

Recovery codes are ten random single-use codes; only their SHA-256 hashes are stored.
"""

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote, urlencode

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1  # steps accepted either side of the current one
ISSUER = "Outreach360"
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i


def generate_secret() -> str:
    """160-bit random secret, base32 without padding (32 characters)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def now_code(secret: str) -> str:
    return code_at(secret, current_step())


def normalize_code(code: Optional[str]) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def matching_step(secret: str, code: Optional[str], last_step: Optional[int] = None,
                  now: Optional[float] = None) -> Optional[int]:
    """The time step the code belongs to (within ±WINDOW), or None. Steps <= last_step are refused."""
    code = normalize_code(code)
    if not secret or len(code) != DIGITS:
        return None
    now_step = current_step(now)
    found = None
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):
        # compare every candidate (no early exit) so timing does not reveal which step matched
        if hmac.compare_digest(code_at(secret, step), code):
            found = step
    if found is None or (last_step is not None and found <= last_step):
        return None
    return found


def provisioning_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    label = quote(f"{issuer}:{account}", safe="@:")
    params = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                        "digits": DIGITS, "period": STEP_SECONDS})
    return f"otpauth://totp/{label}?{params}"


# ── Recovery codes ────────────────────────────────────────

def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(_normalize_recovery(code).encode("utf-8")).hexdigest()


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> tuple[list[str], str]:
    """Return (plain codes to show once, JSON list of their hashes to store)."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, json.dumps([hash_recovery_code(c) for c in codes])


def consume_recovery_code(stored_json: Optional[str], code: str) -> Optional[str]:
    """If the code matches an unused recovery code, return the updated JSON (code removed), else None."""
    if not stored_json or not _normalize_recovery(code):
        return None
    try:
        hashes = list(json.loads(stored_json))
    except (ValueError, TypeError):
        return None
    candidate = hash_recovery_code(code)
    match = None
    for h in hashes:
        if hmac.compare_digest(str(h), candidate):
            match = h
    if match is None:
        return None
    hashes.remove(match)
    return json.dumps(hashes)


def remaining_recovery_codes(stored_json: Optional[str]) -> int:
    try:
        return len(json.loads(stored_json)) if stored_json else 0
    except (ValueError, TypeError):
        return 0
