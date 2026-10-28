"""Primary-key ids (chunk D4): time-ordered UUIDv7 strings.

``new_id()`` returns a 36-character UUID string (RFC 9562 version 7): a 48-bit Unix timestamp in milliseconds
followed by random bits, so ids created later sort later and new rows land at the right-hand end of the
primary-key B-tree instead of at random pages (UUIDv4). Existing ids are not changed; v4 and v7 ids coexist
in the same ``VARCHAR(36)`` columns.

Layout (RFC 9562 section 5.7, monotonicity per section 6.2 "Method 1", a fixed-length dedicated counter, as in
CPython 3.14's ``uuid.uuid7``)::

    unix_ts_ms (48) | ver=7 (4) | counter high (12) | var=0b10 (2) | counter low (30) | random (32)

The 42-bit counter is re-seeded with random bits (top bit clear, leaving room to count up) at every new
millisecond and incremented within one, so ids from one process are strictly increasing. If the counter
overflows, or the wall clock goes backwards, the timestamp is advanced past the last one used instead.

Not for secrets: a UUIDv7 reveals its creation time and has 32-74 random bits. Tokens that must be
unguessable keep ``secrets`` / ``uuid4``.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

__all__ = ["build_uuid7", "new_id", "new_uuid7", "short_id", "uuid7_timestamp_ms"]

_COUNTER_BITS = 42
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1
_MAX_TIMESTAMP_MS = (1 << 48) - 1

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _seed_counter() -> int:
    return secrets.randbits(_COUNTER_BITS - 1)  # top bit clear: at least 2**41 increments before overflow


def _next_timestamp_and_counter() -> tuple[int, int]:
    global _last_ms, _counter
    now = _now_ms()
    with _lock:
        if now > _last_ms:
            _last_ms, _counter = now, _seed_counter()
        else:  # same millisecond, or the clock went back: keep counting on the last timestamp
            _counter += 1
            if _counter > _COUNTER_MAX:
                _last_ms, _counter = _last_ms + 1, _seed_counter()
        return _last_ms, _counter


def build_uuid7(timestamp_ms: int, counter: int, random32: int) -> uuid.UUID:
    """The UUIDv7 for these field values (pure; ``new_uuid7`` supplies them)."""
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP_MS:
        raise ValueError("timestamp_ms out of range for UUIDv7")
    if not 0 <= counter <= _COUNTER_MAX or not 0 <= random32 < (1 << 32):
        raise ValueError("counter or random bits out of range for UUIDv7")
    value = timestamp_ms << 80
    value |= 0x7 << 76                          # version 7
    value |= (counter >> 30) << 64              # rand_a: counter high 12 bits
    value |= 0b10 << 62                         # variant (RFC 9562)
    value |= (counter & ((1 << 30) - 1)) << 32  # rand_b: counter low 30 bits ...
    value |= random32                           # ... then 32 random bits
    return uuid.UUID(int=value)


def new_uuid7() -> uuid.UUID:
    """A new UUIDv7, strictly greater than the previous one from this process."""
    timestamp_ms, counter = _next_timestamp_and_counter()
    return build_uuid7(timestamp_ms, counter, secrets.randbits(32))


def new_id() -> str:
    """A new primary key: a time-ordered UUIDv7 as the canonical 36-character lower-case string."""
    return str(new_uuid7())


def uuid7_timestamp_ms(value: str | uuid.UUID) -> int:
    """The Unix time in milliseconds a UUIDv7 was created at."""
    parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    if parsed.version != 7:
        raise ValueError("not a UUIDv7")
    return parsed.int >> 80


def short_id(value: str, length: int = 8) -> str:
    """A short form of an id for file names, references and logs: its last characters. A UUIDv7 starts
    with its timestamp, so ids made within about a minute share their first 8 characters; the end is random."""
    return value[-length:]
