# app/models/imap_sync_state.py
"""
Per-mailbox, per-folder IMAP sync position (B11).

Each sync remembers the highest UID it has seen in a folder together with the
folder's UIDVALIDITY, so the next sync fetches only newer messages instead of
re-downloading the whole recent window every few minutes. If the server's
UIDVALIDITY changes (folder recreated / renumbered) the stored UID is
meaningless and the sync falls back to a date window once.
"""

from sqlalchemy import Column, String, BigInteger, TIMESTAMP
from sqlalchemy.sql import func

from app.models.base import Base


class ImapSyncState(Base):
    __tablename__ = "imap_sync_state"

    # sending_inboxes.inbox_id (no FK constraint: rows of a deleted inbox are inert)
    inbox_id = Column(String(36), primary_key=True)
    folder = Column(String(255), primary_key=True)
    uidvalidity = Column(BigInteger, nullable=True)
    last_uid = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<ImapSyncState {self.inbox_id[:8]} {self.folder} uid={self.last_uid}>"
