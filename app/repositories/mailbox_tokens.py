"""
Microsoft 365 mailbox token persistence (ADR 0009; T-1): the OAuth token columns of a sending inbox,
written in their own short transaction so the token HTTP call never holds the caller's transaction.
Inbox ids are global UUIDs; the caller has loaded the inbox in its tenant.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.sending_inbox import SendingInbox

__all__ = ["Session", "end_read_transaction", "mirror_committed", "save_token_fields"]


def save_token_fields(session_factory: Callable[[], Session], inbox_id: str, fields: dict) -> None:
    """UPDATE the inbox's token columns in a session of their own, and commit it."""
    with session_factory() as token_db:
        token_db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).update(
            fields, synchronize_session=False)
        token_db.commit()


def mirror_committed(entity: Any, name: str, value: Any) -> None:
    """Set an attribute on a loaded object as if it had been loaded with it (no pending change)."""
    set_committed_value(entity, name, value)


def end_read_transaction(db: Optional[Session]) -> None:
    """Ends the caller's transaction when it holds only reads, keeping its objects loaded."""
    if db is None or not db.in_transaction() or db.new or db.dirty or db.deleted:
        return
    keep = db.expire_on_commit
    db.expire_on_commit = False
    try:
        db.commit()  # nothing pending: this only releases the snapshot / connection
    finally:
        db.expire_on_commit = keep
