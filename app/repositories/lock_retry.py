"""
Run a whole unit of work again after a MySQL lock conflict (D1).

InnoDB resolves a deadlock by rolling back one of the transactions (error 1213, "try restarting
transaction"); a lock wait that exceeds ``innodb_lock_wait_timeout`` fails the statement (1205). Both are
safe to retry *from the start of the transaction*: ``retry_on_lock_conflict(work)`` calls ``work`` again,
where ``work`` is a service method body that opens its unit of work (``with self.uow as uow: ...
uow.commit()``). The unit of work rolls the session back on the way out of the failed attempt, so the next
attempt starts clean and reloads what it reads.

Never wrap only part of a transaction: statements that ran before the failure were rolled back with it.
``work`` must not have side effects outside the database before it commits (e-mails, webhooks sent
directly, files) -- the services this is used for (contact merge, erasure, purges) have none.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError

__all__ = ["LOCK_CONFLICT_ERRORS", "LOCK_RETRY_ATTEMPTS", "is_lock_conflict", "retry_on_lock_conflict"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 1213 deadlock, 1205 lock wait timeout; 1305 "savepoint does not exist" surfaces when the deadlock
# hit inside a savepoint (MySQL dropped the whole transaction, savepoint included)
LOCK_CONFLICT_ERRORS = (1213, 1205, 1305)
LOCK_RETRY_ATTEMPTS = 3
_BASE_PAUSE_S = 0.05


def is_lock_conflict(exc: BaseException) -> bool:
    """A MySQL deadlock / lock wait timeout after which the whole transaction may run again."""
    if not isinstance(exc, OperationalError):
        return False
    args = getattr(exc.orig, "args", None) or (None,)
    return args[0] in LOCK_CONFLICT_ERRORS


def retry_on_lock_conflict(work: Callable[[], T], *, attempts: int = LOCK_RETRY_ATTEMPTS,
                           sleep: Callable[[float], None] = time.sleep) -> T:
    """``work()``; on a lock conflict, pause briefly (growing, jittered) and run it again, at most
    ``attempts`` times in all. Any other error, or the last conflict, is raised unchanged."""
    for attempt in range(1, attempts + 1):
        try:
            return work()
        except OperationalError as exc:
            if attempt == attempts or not is_lock_conflict(exc):
                raise
            # jitter so two retried transactions do not collide again in step (not security-relevant)
            pause = random.uniform(_BASE_PAUSE_S, 2 * _BASE_PAUSE_S) * attempt
            logger.warning("lock conflict (MySQL %s) in %s, attempt %d of %d; retrying in %.2fs",
                           exc.orig.args[0] if exc.orig is not None else "?",
                           getattr(work, "__qualname__", "unit of work"), attempt, attempts, pause)
            sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover  (the loop returns or raises)
