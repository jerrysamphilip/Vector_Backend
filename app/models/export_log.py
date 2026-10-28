"""
Export audit log (migration 0025, gap list C-07): who exported which records, with which view or
filter, how many rows, in which format and when. Admins read it at GET /exports/logs.
No personal data: the filter summary describes the view, never the rows.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class ExportLog(Base):
    __tablename__ = "export_logs"

    export_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)  # CONTACT / LIST / COMPANY
    filter_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    format: Mapped[str] = mapped_column(String(10), nullable=False)  # csv / xlsx
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (
        Index("ix_export_logs_tenant_created", "tenant_id", "created_at"),
        {"mysql_engine": MYSQL_ENGINE, "mysql_charset": MYSQL_CHARSET, "mysql_collate": MYSQL_COLLATION},
    )
