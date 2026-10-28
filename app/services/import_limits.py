# app/services/import_limits.py
"""
Guards for files and rows coming from imports and uploads (CRM import, prospect upload).

- column_limits / oversized_fields: a value longer than its column would fail the whole
  statement (MySQL strict mode: DataError), so it is reported per row, with the field, instead.
  The limits come from the SQLAlchemy model, so they follow schema changes.
- decode_csv: UTF-8 (with or without BOM), else Windows-1252 (Excel's "CSV" on Windows);
  bytes that are neither are refused instead of being replaced silently.
- check_xlsx_archive / check_column_count: an XLSX is a zip; a small file can expand to
  gigabytes (zip bomb) or hold thousands of columns, so both are checked before pandas loads it.
  These file guards live in app/utils/upload_guards.py (shared with app/utils/excel_reader.py) and
  are re-exported here.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.utils.upload_guards import (
    MAX_COLUMNS,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    FileRejected,
    check_column_count,
    check_size,
    check_xlsx_archive,
    decode_csv,
)

__all__ = [
    "MAX_COLUMNS", "MAX_XLSX_UNCOMPRESSED_BYTES", "FileRejected", "check_column_count", "check_size",
    "check_xlsx_archive", "column_limits", "decode_csv", "oversized_fields",
]


def column_limits(model: Any, fields: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """{column name: maximum characters} for the model's length-limited text columns."""
    wanted = set(fields) if fields is not None else None
    limits: Dict[str, int] = {}
    for column in model.__table__.columns:
        length = getattr(column.type, "length", None)
        if isinstance(length, int) and length > 0 and (wanted is None or column.name in wanted):
            limits[column.name] = length
    return limits


def oversized_fields(values: Mapping[str, Any], limits: Mapping[str, int],
                     labels: Optional[Mapping[str, str]] = None) -> List[str]:
    """One message per text value longer than its column allows (lists / dicts are not checked)."""
    problems = []
    for field, value in values.items():
        limit = limits.get(field)
        if not limit or value is None or isinstance(value, (list, dict)):
            continue
        length = len(str(value))
        if length > limit:
            label = (labels or {}).get(field, field)
            problems.append(f"{label} is too long ({length} characters, maximum {limit})")
    return problems
