from sqlalchemy import MetaData
from sqlalchemy.orm import declarative_base

from app.core.db_charset import MYSQL_CHARSET, MYSQL_COLLATION, MYSQL_ENGINE

Base = declarative_base()


def apply_table_defaults(metadata: MetaData) -> None:
    """Give every registered table the one engine, charset and collation (app/core/db_charset.py)."""
    for table in metadata.tables.values():
        table.dialect_kwargs["mysql_engine"] = MYSQL_ENGINE
        table.dialect_kwargs["mysql_charset"] = MYSQL_CHARSET
        table.dialect_kwargs["mysql_collate"] = MYSQL_COLLATION
