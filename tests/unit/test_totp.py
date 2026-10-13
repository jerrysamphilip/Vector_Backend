"""RFC 6238 TOTP and recovery codes (app/core/totp.py), checked against the RFC's SHA-1 test vectors."""
import base64
import json
import re

import pytest

from app.core import totp

# RFC 6238 appendix B: ASCII secret "12345678901234567890", SHA-1. The 8-digit reference values are
# truncated to the 6 digits the app uses (value mod 10**6).
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
RFC_VECTORS = [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
               (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")]


@pytest.mark.parametrize("unix_time,code", RFC_VECTORS)
def test_rfc6238_vectors(unix_time, code):
    assert totp.code_at(RFC_SECRET, totp.current_step(unix_time)) == code


def test_secret_key_tolerates_spaces_and_lower_case():
    spaced = " ".join(RFC_SECRET[i:i + 4] for i in range(0, len(RFC_SECRET), 4)).lower()
    assert totp.code_at(spaced, 1) == "287082"


def test_matching_step_accepts_one_step_of_drift_either_side():
    assert totp.matching_step(RFC_SECRET, "287082", now=59) == 1
    assert totp.matching_step(RFC_SECRET, totp.code_at(RFC_SECRET, 0), now=59) == 0
    assert totp.matching_step(RFC_SECRET, totp.code_at(RFC_SECRET, 2), now=59) == 2
    assert totp.matching_step(RFC_SECRET, totp.code_at(RFC_SECRET, 3), now=59) is None


def test_matching_step_refuses_replays():
    assert totp.matching_step(RFC_SECRET, "287082", last_step=1, now=59) is None
    assert totp.matching_step(RFC_SECRET, "287082", last_step=0, now=59) == 1


@pytest.mark.parametrize("code", ["", None, "12345", "1234567", "abcdef"])
def test_matching_step_rejects_malformed_codes(code):
    assert totp.matching_step(RFC_SECRET, code, now=59) is None


def test_matching_step_normalises_separators_and_needs_a_secret():
    assert totp.matching_step(RFC_SECRET, "287 082", now=59) == 1
    assert totp.matching_step(RFC_SECRET, "287-082", now=59) == 1
    assert totp.matching_step("", "287082", now=59) is None


def test_generated_secret_is_160_bit_base32():
    secret = totp.generate_secret()
    assert re.fullmatch(r"[A-Z2-7]{32}", secret)
    assert len(base64.b32decode(secret)) == 20
    assert totp.generate_secret() != secret


def test_provisioning_uri():
    uri = totp.provisioning_uri("ABCDEFGH", "pat@x.example")
    assert uri.startswith("otpauth://totp/Outreach360:pat@x.example?")
    for part in ("secret=ABCDEFGH", "issuer=Outreach360", "algorithm=SHA1", "digits=6", "period=30"):
        assert part in uri


def test_recovery_codes_shape_and_storage():
    codes, stored = totp.generate_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODE_COUNT == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert re.fullmatch(r"[a-z2-9]{5}-[a-z2-9]{5}", c)
        assert not set(c.replace("-", "")) - set(totp._RECOVERY_ALPHABET)
    hashes = json.loads(stored)
    assert len(hashes) == 10 and codes[0] not in stored
    assert totp.remaining_recovery_codes(stored) == 10


def test_recovery_code_is_single_use_and_format_tolerant():
    codes, stored = totp.generate_recovery_codes(3)
    after = totp.consume_recovery_code(stored, codes[1].upper().replace("-", " "))
    assert after is not None and totp.remaining_recovery_codes(after) == 2
    assert totp.consume_recovery_code(after, codes[1]) is None
    assert totp.consume_recovery_code(after, codes[0]) is not None


@pytest.mark.parametrize("stored,code", [(None, "abcde-fghjk"), ("", "abcde-fghjk"), ("not json", "abcde-fghjk"),
                                         ('["x"]', ""), ('["x"]', "---")])
def test_recovery_code_bad_inputs(stored, code):
    assert totp.consume_recovery_code(stored, code) is None


def test_remaining_recovery_codes_bad_json():
    assert totp.remaining_recovery_codes(None) == 0
    assert totp.remaining_recovery_codes("{broken") == 0
