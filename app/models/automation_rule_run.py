"""
Which contacts an automation rule has already acted on (migration 0021).

A rule fires on every matching event (every open of an email, for example), so without a record the
same automation email would be queued again and again, and two events arriving together would both
pass a check-then-insert. One row per (rule, contact) under a unique key makes "once per rule and
contact" a database guarantee. No foreign keys on purpose: the rows are a small ledger and must
never block deleting a rule or a contact.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class AutomationRuleRun(Base):
    __tablename__ = "automation_rule_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    prospect_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (
        UniqueConstraint("rule_id", "prospect_id", name="uq_automation_rule_runs_rule_prospect"),
        # Set here as well as by apply_table_defaults: the table is complete even when this module
        # is imported after app.models has applied the defaults
        {"mysql_engine": MYSQL_ENGINE, "mysql_charset": MYSQL_CHARSET, "mysql_collate": MYSQL_COLLATION},
    )

    def __repr__(self) -> str:
        return f"<AutomationRuleRun rule={self.rule_id} prospect={self.prospect_id}>"
