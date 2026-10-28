"""S1 functional: sessions on PyJWT accept tokens minted the python-jose way and keep the policies."""
import base64
import calendar
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from tests.conftest import auth, login, register_tenant


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jose_style(claims: dict, alg: str = "HS256", digest=hashlib.sha256) -> str:
    enc = lambda obj: _b64(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode())  # noqa: E731
    signing_input = f"{enc({'alg': alg, 'typ': 'JWT'})}.{enc(claims)}"
    sig = hmac.new(settings.JWT_SECRET_KEY.encode(), signing_input.encode(), digest).digest()
    return f"{signing_input}.{_b64(sig)}"


def _claims(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def _exp(minutes: int) -> int:
    return calendar.timegm((datetime.now(timezone.utc) + timedelta(minutes=minutes)).utctimetuple())


def test_old_jose_token_authenticates_and_logout_still_revokes_it(client):
    owner = register_tenant(client, "s1old")
    issued = _claims(owner["token"])
    old = _jose_style({"sub": issued["sub"], "tenant_id": issued["tenant_id"], "role": issued["role"],
                       "exp": _exp(30), "type": "access", "jti": issued["jti"] + "x"})
    r = client.get("/api/auth/me", headers=auth(old))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == owner["email"]

    assert client.post("/api/auth/logout", headers=auth(old)).status_code == 204
    assert client.get("/api/auth/me", headers=auth(old)).status_code == 401


def test_alg_none_other_hmac_and_expired_tokens_get_401(client):
    owner = register_tenant(client, "s1alg")
    base = {k: v for k, v in _claims(login(client, owner["email"], owner["password"])).items()}
    enc = lambda obj: _b64(json.dumps(obj).encode())  # noqa: E731
    unsigned = f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(base)}."
    hs512 = _jose_style(base, "HS512", hashlib.sha512)
    expired = _jose_style({**base, "exp": _exp(-1)})
    no_jti = _jose_style({k: v for k, v in base.items() if k != "jti"})
    for bad in (unsigned, hs512, expired, no_jti):
        assert client.get("/api/auth/me", headers=auth(bad)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(_jose_style(base))).status_code == 200
