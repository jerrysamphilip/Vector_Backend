# app/models/status_checks.py
"""
CHECK constraints for status columns (chunk D5): the models declare the same constraints that alembic
0029_status_checks adds to existing databases, from the shared vocabularies in app/domain/status.py, so a fresh
build (0001_baseline's create_all) and a migrated database end up identical.
"""

from sqlalchemy import CheckConstraint

from app.domain.status import check_for


def status_check(table: str, column: str) -> CheckConstraint:
    """``CONSTRAINT ck_<table>_<column> CHECK (<column> IN (...))``."""
    name, sql = check_for(table, column)
    return CheckConstraint(sql, name=name)
