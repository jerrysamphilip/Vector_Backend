"""
Which mailbox gets the next contact when a campaign's emails are pre-scheduled or its mailboxes
change (S-18). Pure: no I/O.

Mailboxes are used in proportion to their effective daily limit (``SendingInbox.current_daily_limit``:
override, warm-up ramp or daily_limit), so a mailbox that can send 2,000 a day gets four times as many
contacts as one that can send 500 and both reach their cap at about the same time. Smooth weighted
round-robin: every prefix of the sequence stays within one contact of each mailbox's ideal share, the
order is deterministic (ties go to the lowest index), and equal weights give plain round-robin
(0, 1, 2, 0, 1, 2, ...), what was used before.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def inbox_weight(inbox: Any) -> int:
    """A mailbox's effective daily limit; 0 when unknown or negative."""
    try:
        return max(int(getattr(inbox, "current_daily_limit", None) or 0), 0)
    except (TypeError, ValueError):
        return 0


class WeightedRotation:
    """Smooth weighted round-robin over ``len(weights)`` slots."""

    def __init__(self, weights: Sequence[int]) -> None:
        if not weights:
            raise ValueError("at least one mailbox is needed")
        clean = [max(int(w or 0), 0) for w in weights]
        # Nothing to weigh by (every limit 0): share equally rather than refuse
        self._weights = clean if any(clean) else [1] * len(clean)
        self._total = sum(self._weights)
        self._current = [0] * len(self._weights)

    def next_index(self) -> int:
        for i, weight in enumerate(self._weights):
            self._current[i] += weight
        chosen = max(range(len(self._weights)), key=lambda i: (self._current[i], -i))
        self._current[chosen] -= self._total
        return chosen


class InboxRotation(Generic[T]):
    """Hands out mailboxes in weighted order: ``next()`` is the mailbox for the next new contact."""

    def __init__(self, inboxes: Iterable[T]) -> None:
        self._inboxes = list(inboxes)
        self._rotation = WeightedRotation([inbox_weight(i) for i in self._inboxes]) if self._inboxes else None

    def __bool__(self) -> bool:
        return bool(self._inboxes)

    def next(self) -> T | None:
        """The next mailbox; None when there are no mailboxes."""
        if self._rotation is None:
            return None
        return self._inboxes[self._rotation.next_index()]

    def take(self) -> T:
        """The next mailbox; ValueError when there are none (callers check ``bool(rotation)`` first)."""
        if self._rotation is None:
            raise ValueError("no mailbox to rotate over")
        return self._inboxes[self._rotation.next_index()]


def weighted_sequence(weights: Sequence[int], count: int) -> list[int]:
    """Slot index for each of ``count`` consecutive contacts."""
    if count <= 0 or not weights:
        return []
    rotation = WeightedRotation(weights)
    return [rotation.next_index() for _ in range(count)]
