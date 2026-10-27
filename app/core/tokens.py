"""
Signed-token codec (chunk S1, docs/REFACTOR_PLAN.md): the single place that encodes and decodes JWTs.

Auth code (app.core.security, OAuth ``state`` parameters in app.services.ms365_oauth / account_sync)
calls only ``encode`` / ``decode`` here; the library behind them (PyJWT) is an implementation detail
behind the ``TokenCodec`` Protocol so S2 can swap or wrap it (``set_codec``).

Policy kept from the python-jose implementation (tokens minted before the switch stay valid):
- HMAC with the shared ``JWT_SECRET_KEY`` and ``JWT_ALGORITHM`` (HS256); header ``{"alg", "typ": "JWT"}``.
- Algorithm pinning: only the configured algorithm is accepted. ``none``, any other HMAC size and every
  asymmetric algorithm are refused, so alg-confusion (e.g. an RS256/HS256 public-key swap) cannot apply;
  a configured algorithm outside the HMAC family is refused outright.
- No clock-skew leeway (``LEEWAY_SECONDS = 0``, python-jose's default).
- ``exp`` is required on every token (every token the app has ever minted carries one); callers add
  their own required claims (e.g. ``sub``, ``type``, ``jti`` for access tokens).
- Library errors never escape: every failure raises ``TokenError``.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

import jwt

from app.core.config import settings

LEEWAY_SECONDS = 0
HMAC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
ALWAYS_REQUIRED: tuple[str, ...] = ("exp",)


class TokenError(Exception):
    """A token is malformed, badly signed, expired, of the wrong algorithm or missing a required claim."""


class TokenCodec(Protocol):
    def encode(self, claims: Mapping[str, Any]) -> str: ...

    def decode(self, token: str, require: Sequence[str] = ()) -> dict[str, Any]: ...


def _algorithm() -> str:
    alg = settings.JWT_ALGORITHM
    if alg not in HMAC_ALGORITHMS:
        raise TokenError(f"JWT_ALGORITHM must be one of {sorted(HMAC_ALGORITHMS)}")
    return alg


class PyJWTCodec:
    """HMAC JWTs via PyJWT. Reads the secret and algorithm from settings at call time."""

    def encode(self, claims: Mapping[str, Any]) -> str:
        return jwt.encode(dict(claims), settings.JWT_SECRET_KEY, algorithm=_algorithm())

    def decode(self, token: str, require: Sequence[str] = ()) -> dict[str, Any]:
        required = list(dict.fromkeys((*ALWAYS_REQUIRED, *require)))
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[_algorithm()],
                leeway=LEEWAY_SECONDS,
                options={"require": required, "verify_signature": True, "verify_exp": True},
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise TokenError(str(exc) or exc.__class__.__name__) from exc
        if not isinstance(payload, dict):
            raise TokenError("Invalid payload")
        return payload


_codec: TokenCodec = PyJWTCodec()


def set_codec(codec: TokenCodec) -> TokenCodec:
    """Replace the process-wide codec (S2 DI / tests); returns the previous one."""
    global _codec
    previous, _codec = _codec, codec
    return previous


def encode(claims: Mapping[str, Any]) -> str:
    return _codec.encode(claims)


def decode(token: str, require: Sequence[str] = ()) -> dict[str, Any]:
    return _codec.decode(token, require)
