"""Unit tests for app.core.ids (chunk D4): UUIDv7 format, version / variant bits, ordering, monotonicity."""
import re
import threading
import uuid

import pytest

from app.core import ids
from app.core.ids import build_uuid7, new_id, new_uuid7, uuid7_timestamp_ms

CANONICAL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_dbb_ids_u01_format_is_canonical_36_char_v7():
    value = new_id()
    assert isinstance(value, str) and len(value) == 36
    assert CANONICAL.fullmatch(value), value
    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    assert str(parsed) == value


def test_dbb_ids_u02_timestamp_is_now(monkeypatch):
    monkeypatch.setattr(ids, "_now_ms", lambda: 1_767_225_600_123)  # 2026-01-01T00:00:00.123Z
    monkeypatch.setattr(ids, "_last_ms", -1)
    assert uuid7_timestamp_ms(new_id()) == 1_767_225_600_123


def test_dbb_ids_u03_build_places_fields_per_rfc9562():
    # RFC 9562 appendix A.6 example timestamp 0x017F22E279B0 (2022-02-22T19:22:22Z)
    value = build_uuid7(0x017F22E279B0, 0, 0)
    assert str(value) == "017f22e2-79b0-7000-8000-000000000000"
    value = build_uuid7(0x017F22E279B0, (1 << 42) - 1, (1 << 32) - 1)
    assert str(value) == "017f22e2-79b0-7fff-bfff-ffffffffffff"
    assert value.version == 7 and value.variant == uuid.RFC_4122


@pytest.mark.parametrize("args", [(-1, 0, 0), (1 << 48, 0, 0), (0, 1 << 42, 0), (0, 0, 1 << 32), (0, -1, 0)])
def test_dbb_ids_u04_build_rejects_out_of_range(args):
    with pytest.raises(ValueError):
        build_uuid7(*args)


def test_dbb_ids_u05_strictly_increasing_within_one_millisecond(monkeypatch):
    monkeypatch.setattr(ids, "_now_ms", lambda: 1_767_225_600_000)
    monkeypatch.setattr(ids, "_last_ms", -1)
    values = [new_id() for _ in range(5000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)
    assert {uuid7_timestamp_ms(v) for v in values} == {1_767_225_600_000}


def test_dbb_ids_u06_ordered_across_milliseconds_and_string_sort_matches(monkeypatch):
    clock = iter(range(1_767_225_600_000, 1_767_225_600_000 + 1000))
    monkeypatch.setattr(ids, "_now_ms", lambda: next(clock))
    monkeypatch.setattr(ids, "_last_ms", -1)
    values = [new_uuid7() for _ in range(1000)]
    assert values == sorted(values, key=lambda u: u.int)
    assert [str(v) for v in values] == sorted(str(v) for v in values)  # VARCHAR order == creation order


def test_dbb_ids_u07_clock_going_backwards_stays_monotonic(monkeypatch):
    times = iter([2_000, 2_000, 1_500, 1_000, 2_001])
    monkeypatch.setattr(ids, "_now_ms", lambda: next(times))
    monkeypatch.setattr(ids, "_last_ms", -1)
    values = [new_uuid7() for _ in range(5)]
    assert values == sorted(values, key=lambda u: u.int)
    assert [uuid7_timestamp_ms(v) for v in values] == [2_000, 2_000, 2_000, 2_000, 2_001]


def test_dbb_ids_u08_counter_overflow_advances_timestamp(monkeypatch):
    monkeypatch.setattr(ids, "_now_ms", lambda: 5_000)
    monkeypatch.setattr(ids, "_last_ms", 5_000)
    monkeypatch.setattr(ids, "_counter", (1 << 42) - 1)
    value = new_uuid7()
    assert uuid7_timestamp_ms(value) == 5_001


def test_dbb_ids_u09_threads_get_unique_ids():
    out: list[str] = []
    lock = threading.Lock()

    def work():
        local = [new_id() for _ in range(2000)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 16000


def test_dbb_ids_u10_timestamp_of_non_v7_is_refused():
    with pytest.raises(ValueError):
        uuid7_timestamp_ms(str(uuid.uuid4()))


def test_short_id_is_the_random_end():
    from app.core.ids import short_id
    a = str(ids.build_uuid7(1_700_000_000_000, 1, 0x11111111))
    b = str(ids.build_uuid7(1_700_000_000_000, 2, 0x22222222))
    assert a[:8] == b[:8] and short_id(a) != short_id(b)  # same moment: the start is shared, the end is not
    assert short_id("0123456789abcdef") == "89abcdef" and short_id("abc") == "abc"
