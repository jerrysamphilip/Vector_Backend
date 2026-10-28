# app/models/import_mapping.py
"""
Saved column mappings for the CRM import (C-04, migration 0026): a workspace names a mapping
{column header: target} with its options (mode, date order, default source) and reuses it for the
next file with the same layout. Names are unique per workspace.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class ImportMapping(Base):
    __tablename__ = "import_mappings"

    mapping_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mapping: Mapped[Any] = mapped_column(JSON, nullable=False)
    options: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="SET NULL"),
                                                      nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6,
                                                           nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_import_mappings_tenant_name"),
        {"mysql_engine": MYSQL_ENGINE, "mysql_charset": MYSQL_CHARSET, "mysql_collate": MYSQL_COLLATION},
    )
