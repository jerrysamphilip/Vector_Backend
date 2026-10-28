# app/models/imap_sync_state.py
"""
Per-mailbox, per-folder IMAP sync position (B11).

Each sync remembers the highest UID it has seen in a folder together with the
folder's UIDVALIDITY, so the next sync fetches only newer messages instead of
re-downloading the whole recent window every few minutes. If the server's
UIDVALIDITY changes (folder recreated / renumbered) the stored UID is
meaningless and the sync falls back to a date window once.
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import short_id


class ImapSyncState(Base):
    __tablename__ = "imap_sync_state"

    # sending_inboxes.inbox_id (no FK constraint: rows of a deleted inbox are inert)
    inbox_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    folder: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)
    uidvalidity: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_uid: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ImapSyncState {short_id(self.inbox_id)} {self.folder} uid={self.last_uid}>"
