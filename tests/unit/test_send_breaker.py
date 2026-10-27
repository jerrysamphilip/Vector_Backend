"""Circuit breaker and sender-rejection counting (item 14), with a fake clock."""
from app.services.send_breaker import CircuitBreaker, RejectionTracker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_send_breaker_opens_after_consecutive_failures_and_cools_down():
    clock = Clock()
    b = CircuitBreaker(threshold=5, cooldown=300, clock=clock)
    for _ in range(4):
        assert b.record_failure() is False
    assert not b.is_open()
    assert b.record_failure() is True
    assert b.is_open() and 299 < b.retry_in() <= 300
    clock.t += 299
    assert b.is_open()
    clock.t += 2
    assert not b.is_open()  # half-open: one probe allowed


def test_send_breaker_success_resets_the_count():
    b = CircuitBreaker(threshold=3, cooldown=60, clock=Clock())
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert not b.is_open()


def test_send_breaker_half_open_failure_reopens_at_once_and_success_closes():
    clock = Clock()
    b = CircuitBreaker(threshold=2, cooldown=10, clock=clock)
    b.record_failure()
    b.record_failure()
    clock.t += 11
    assert not b.is_open()
    assert b.record_failure() is True  # the probe failed
    assert b.is_open()
    clock.t += 11
    assert not b.is_open()
    b.record_success()
    assert b.record_failure() is False and not b.is_open()


def test_send_rejections_count_per_inbox_in_a_sliding_window():
    clock = Clock()
    t = RejectionTracker(threshold=3, window=1800, clock=clock)
    assert not t.record("a")
    assert not t.record("b")
    clock.t += 1000
    assert not t.record("a")
    clock.t += 900  # the first "a" left the window
    assert not t.record("a")
    assert t.record("a")  # three within 30 minutes
    assert not t.record("a")  # counting starts again after a pause
