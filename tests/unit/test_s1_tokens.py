"""S1: session / OAuth-state tokens behind app.core.tokens (PyJWT), replacing python-jose.

Pure logic: no DB. Covers encode/decode, algorithm pinning (none / alg confusion), expiry with zero
leeway, wrong token type, required claims, and that tokens minted by the old python-jose code path
(same header, claims and HMAC) still validate.
"""
import base64
import calendar
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.core import security, tokens
from app.core.config import settings
from app.core.tokens import TokenError


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jose_style(claims: dict, header: dict | None = None, key: str | None = None, digest=hashlib.sha256) -> str:
    """Mint a token exactly like python-jose 3.5 did: compact JSON, sorted keys, HMAC over b64 parts."""
    header = header if header is not None else {"alg": "HS256", "typ": "JWT"}
    enc = lambda obj: _b64(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))  # noqa: E731
    signing_input = f"{enc(header)}.{enc(claims)}"
    secret = (key or settings.JWT_SECRET_KEY).encode("utf-8")
    sig = hmac.new(secret, signing_input.encode("ascii"), digest).digest()
    return f"{signing_input}.{_b64(sig)}"


def _ts(delta: timedelta) -> int:
    return calendar.timegm((datetime.now(timezone.utc) + delta).utctimetuple())


def _access_claims(**over) -> dict:
    claims = {"sub": "user-1", "tenant_id": "tenant-1", "role": "AGENT", "exp": _ts(timedelta(minutes=30)),
              "type": "access", "jti": secrets.token_urlsafe(16)}
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


# ── Encode / decode ─────────────────────────────────────

def test_access_token_round_trip_keeps_claim_names():
    tok = security.create_access_token("u-1", "t-1", "ADMIN")
    payload = security.decode_access_token(tok)
    assert {k: payload[k] for k in ("sub", "tenant_id", "role", "type")} == {
        "sub": "u-1", "tenant_id": "t-1", "role": "ADMIN", "type": "access"}
    assert isinstance(payload["exp"], int) and isinstance(payload["jti"], str) and payload["jti"]
    header = json.loads(base64.urlsafe_b64decode(tok.split(".")[0] + "=="))
    assert header == {"alg": "HS256", "typ": "JWT"}


def test_access_token_lifetime_is_configured_minutes():
    payload = security.decode_access_token(security.create_access_token("u", "t", "AGENT"))
    expected = _ts(timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    assert abs(payload["exp"] - expected) <= 2


def test_mfa_token_round_trip():
    payload = security.decode_mfa_token(security.create_mfa_token("u-9", first_login=True))
    assert payload["sub"] == "u-9" and payload["type"] == "mfa" and payload["fl"] is True and payload["jti"]


# ── Old python-jose tokens stay valid ───────────────────

def test_token_minted_by_old_jose_code_path_validates():
    claims = _access_claims()
    payload = security.decode_access_token(_jose_style(claims))
    assert payload == claims


def test_old_jose_mfa_token_validates():
    claims = {"sub": "u-2", "type": "mfa", "jti": "j", "fl": False, "exp": _ts(timedelta(minutes=5))}
    assert security.decode_mfa_token(_jose_style(claims)) == claims


def test_old_jose_oauth_state_tokens_validate():
    from app.services import account_sync, ms365_oauth

    ms = {"inbox_id": "i", "tenant_id": "t", "user_id": "u", "purpose": "ms365_oauth", "nonce": "n",
          "exp": _ts(timedelta(minutes=10))}
    assert ms365_oauth.read_state(_jose_style(ms)) == ms
    sync = {"user_id": "u", "provider": "GOOGLE", "purpose": "account_sync", "nonce": None,
            "exp": _ts(timedelta(minutes=10))}
    assert account_sync.read_state(_jose_style(sync)) == sync


def test_new_tokens_verify_with_plain_hmac_like_jose_did():
    """The other direction: a token we mint now carries a standard HS256 signature over the same secret."""
    tok = security.create_access_token("u", "t", "AGENT")
    signing_input, sig = tok.rsplit(".", 1)
    expected = hmac.new(settings.JWT_SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    assert _b64(expected) == sig


# ── Algorithm pinning ───────────────────────────────────

def test_alg_none_is_rejected():
    claims = _access_claims()
    enc = lambda obj: _b64(json.dumps(obj).encode())  # noqa: E731
    for alg in ("none", "None", "NONE"):
        unsigned = f"{enc({'alg': alg, 'typ': 'JWT'})}.{enc(claims)}."
        with pytest.raises(TokenError):
            security.decode_access_token(unsigned)


def test_other_hmac_sizes_with_the_same_secret_are_rejected():
    claims = _access_claims()
    for alg, digest in (("HS384", hashlib.sha384), ("HS512", hashlib.sha512)):
        with pytest.raises(TokenError):
            security.decode_access_token(_jose_style(claims, {"alg": alg, "typ": "JWT"}, digest=digest))


@pytest.mark.parametrize("alg", ["RS256", "ES256", "PS256", "EdDSA"])
def test_asymmetric_header_with_hmac_signature_is_rejected(alg):
    """Alg confusion: an attacker HMAC-signs with a 'public key' and claims an asymmetric alg."""
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(), {"alg": alg, "typ": "JWT"}))


def test_hs256_signed_with_a_public_key_is_rejected():
    fake_pem = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----\n"
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(), key=fake_pem))


@pytest.mark.parametrize("alg", ["none", "RS256", "ES256", ""])
def test_non_hmac_configured_algorithm_is_refused(monkeypatch, alg):
    tok = security.create_access_token("u", "t", "AGENT")
    monkeypatch.setattr(settings, "JWT_ALGORITHM", alg)
    with pytest.raises(TokenError):
        security.decode_access_token(tok)
    with pytest.raises(TokenError):
        security.create_access_token("u", "t", "AGENT")


def test_wrong_secret_and_tampering_are_rejected():
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(), key="another-secret-0123456789abcdef0123"))
    tok = security.create_access_token("u", "t", "AGENT")
    head, body, sig = tok.split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged["role"] = "SUPER_ADMIN"
    with pytest.raises(TokenError):
        security.decode_access_token(f"{head}.{_b64(json.dumps(forged).encode())}.{sig}")


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "a.b", "....", "eyJ.eyJ.sig"])
def test_garbage_raises_token_error_only(garbage):
    with pytest.raises(TokenError):
        security.decode_access_token(garbage)


def test_non_object_payload_is_rejected():
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(b"[1,2,3]")
    sig = hmac.new(settings.JWT_SECRET_KEY.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    with pytest.raises(TokenError):
        security.decode_access_token(f"{head}.{body}.{_b64(sig)}")


# ── Expiry and leeway ───────────────────────────────────

def test_expired_token_is_rejected():
    tok = security.create_access_token("u", "t", "AGENT", expires_delta=timedelta(seconds=-1))
    with pytest.raises(TokenError):
        security.decode_access_token(tok)


def test_leeway_is_zero_as_with_jose():
    assert tokens.LEEWAY_SECONDS == 0
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(exp=_ts(timedelta(seconds=-2)))))
    assert security.decode_access_token(_jose_style(_access_claims(exp=_ts(timedelta(seconds=5)))))


def test_expired_mfa_and_state_tokens_are_rejected():
    from app.services import ms365_oauth
    from app.services.ms365_oauth import OAuthError

    with pytest.raises(TokenError):
        security.decode_mfa_token(_jose_style({"sub": "u", "type": "mfa", "jti": "j",
                                                "exp": _ts(timedelta(seconds=-5))}))
    with pytest.raises(OAuthError):
        ms365_oauth.read_state(_jose_style({"purpose": "ms365_oauth", "exp": _ts(timedelta(seconds=-5))}))


# ── Token type ──────────────────────────────────────────

def test_mfa_token_is_not_an_access_token():
    with pytest.raises(TokenError):
        security.decode_access_token(security.create_mfa_token("u"))


def test_access_token_is_not_an_mfa_token():
    with pytest.raises(TokenError):
        security.decode_mfa_token(security.create_access_token("u", "t", "AGENT"))


def test_oauth_state_is_not_an_access_token_and_vice_versa():
    from app.services import ms365_oauth
    from app.services.ms365_oauth import OAuthError

    with pytest.raises(TokenError):
        security.decode_access_token(ms365_oauth.make_state("i", "t", "u"))
    with pytest.raises(OAuthError):
        ms365_oauth.read_state(security.create_access_token("u", "t", "AGENT"))


@pytest.mark.parametrize("bad_type", ["refresh", "ACCESS", "", 1])
def test_wrong_type_value_is_rejected(bad_type):
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(type=bad_type)))


# ── Required claims ─────────────────────────────────────

@pytest.mark.parametrize("missing", ["exp", "sub", "type", "jti"])
def test_access_token_missing_required_claim_is_rejected(missing):
    claims = _access_claims()
    del claims[missing]
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(claims))


@pytest.mark.parametrize("missing", ["exp", "sub", "type", "jti"])
def test_mfa_token_missing_required_claim_is_rejected(missing):
    claims = {"sub": "u", "type": "mfa", "jti": "j", "fl": False, "exp": _ts(timedelta(minutes=5))}
    del claims[missing]
    with pytest.raises(TokenError):
        security.decode_mfa_token(_jose_style(claims))


def test_state_token_without_exp_is_rejected():
    from app.services import account_sync

    with pytest.raises(account_sync.SyncError):
        account_sync.read_state(_jose_style({"user_id": "u", "provider": "GOOGLE", "purpose": "account_sync"}))


@pytest.mark.parametrize("claim,value", [("sub", 123), ("jti", 5), ("exp", "tomorrow")])
def test_malformed_registered_claims_are_rejected(claim, value):
    with pytest.raises(TokenError):
        security.decode_access_token(_jose_style(_access_claims(**{claim: value})))


# ── Swappable codec (S2) ────────────────────────────────

def test_codec_can_be_swapped_behind_the_protocol():
    calls = []

    class Recording:
        def encode(self, claims):
            calls.append(("encode", dict(claims)))
            return "opaque"

        def decode(self, token, require=()):
            calls.append(("decode", token, tuple(require)))
            return {"type": "access", "sub": "u", "jti": "j", "exp": 1}

    previous = tokens.set_codec(Recording())
    try:
        assert security.create_access_token("u", "t", "AGENT") == "opaque"
        assert security.decode_access_token("opaque")["sub"] == "u"
    finally:
        tokens.set_codec(previous)
    assert calls[0][0] == "encode" and calls[1] == ("decode", "opaque", security.ACCESS_REQUIRED_CLAIMS)
    assert isinstance(previous, tokens.PyJWTCodec)
