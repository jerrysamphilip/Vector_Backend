"""Unit tests (no database) for the shared-counter repositories behind DEC-09: the SQL each method sends and
how it reads the result. The cross-process behaviour itself is covered by tests/test_shared_limits.py."""
from types import SimpleNamespace

from app.repositories.platform_flags import PlatformFlagRepository
from app.repositories.rate_limits import RateLimitRepository


class RecordingConnection:
    """Records (sql, params) per execute and answers with a canned result."""

    def __init__(self, rows=(), rowcount=0, scalar=None):
        self.calls = []
        self._rows, self._rowcount, self._scalar = list(rows), rowcount, scalar

    def execute(self, statement, params=None):
        self.calls.append((" ".join(str(statement).split()), params or {}))
        return SimpleNamespace(all=lambda: self._rows, rowcount=self._rowcount, scalar=lambda: self._scalar,
                               scalar_one_or_none=lambda: self._scalar)

    @property
    def sql(self):
        return [sql for sql, _ in self.calls]


def test_add_hit_upserts_one_hit_in_the_slot():
    conn = RecordingConnection()
    RateLimitRepository(conn).add_hit("b" * 64, 1_700_000_000, 1_700_000_900)
    sql, params = conn.calls[0]
    assert sql.startswith("INSERT INTO rate_limit_counters") and "ON DUPLICATE KEY UPDATE hits = hits + 1" in sql
    assert params == {"b": "b" * 64, "s": 1_700_000_000, "e": 1_700_000_900}


def test_remove_hit_never_goes_below_zero():
    conn = RecordingConnection()
    RateLimitRepository(conn).remove_hit("bkt", 60)
    assert "GREATEST(hits - 1, 0)" in conn.sql[0] and conn.calls[0][1] == {"b": "bkt", "s": 60}


def test_live_slots_returns_int_pairs_after_the_cut_off():
    conn = RecordingConnection(rows=[("120", "3"), (180, 1)])
    assert RateLimitRepository(conn).live_slots("bkt", 100.5) == [(120, 3), (180, 1)]
    assert "slot_start > :a" in conn.sql[0] and conn.calls[0][1] == {"b": "bkt", "a": 100.5}


def test_delete_bucket_and_delete_all():
    conn = RecordingConnection()
    repo = RateLimitRepository(conn)
    repo.delete_bucket("bkt")
    repo.delete_all()
    assert conn.sql == ["DELETE FROM rate_limit_counters WHERE bucket = :b", "DELETE FROM rate_limit_counters"]


def test_purge_expired_deletes_in_batches_and_reports_the_count():
    conn = RecordingConnection(rowcount=7)
    assert RateLimitRepository(conn).purge_expired(1_700_000_000.9, batch=100) == 7
    sql, params = conn.calls[0]
    assert "expires_at <= :n" in sql and "LIMIT :l" in sql and params == {"n": 1_700_000_000, "l": 100}
    assert RateLimitRepository(RecordingConnection(rowcount=None)).purge_expired(1.0) == 0


def test_platform_flag_counters_for_the_daily_limit():
    conn = RecordingConnection(rowcount=1)
    repo = PlatformFlagRepository(conn)
    repo.ensure_counter("dnc:2026-11-06:u1", 5)
    assert conn.sql[-1].startswith("INSERT IGNORE INTO platform_flags")
    assert repo.remove_range("dnc:", "dnc:2026-11-05") == 1
    assert "name LIKE :p AND name < :b" in conn.sql[-1]
    assert repo.increment_below("dnc:2026-11-06:u1", 500) is True
    assert repo.add_within("llm", 2, 10) is True
    assert PlatformFlagRepository(RecordingConnection(rowcount=0)).increment_below("x", 1) is False
    repo.adjust("dnc:2026-11-06:u1", -1)
    assert "GREATEST(CAST(value AS SIGNED) + :d, 0)" in conn.sql[-1] and conn.calls[-1][1] == {"d": -1, "n": "dnc:2026-11-06:u1"}
    repo.remove_range("dnc_x%", "z")
    assert conn.calls[-1][1]["p"] == r"dnc\_x\%%"  # LIKE wildcards in the prefix are escaped
