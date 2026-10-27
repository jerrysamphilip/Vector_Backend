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
"""

import io
import zipfile
from typing import Any, Dict, Iterable, List, Mapping, Optional

MAX_COLUMNS = 200
MAX_XLSX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


class FileRejected(ValueError):
    """The file cannot be imported; the message is safe to show to the user."""


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


def decode_csv(raw: bytes) -> str:
    """CSV bytes as text: UTF-8 (BOM optional), else Windows-1252; anything else is refused."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise FileRejected("The file is not UTF-8 or Windows-1252 text. Save it as CSV UTF-8 and try again.") from exc


def check_xlsx_archive(raw: bytes, max_uncompressed: int = MAX_XLSX_UNCOMPRESSED_BYTES) -> None:
    """Refuse an XLSX that is not a zip or expands to more than max_uncompressed bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise FileRejected("The file is not a valid .xlsx workbook.") from exc
    if total > max_uncompressed:
        raise FileRejected(
            f"The workbook expands to {total // (1024 * 1024)} MB; the limit is {max_uncompressed // (1024 * 1024)} MB.")


def check_column_count(columns: Iterable[Any], max_columns: int = MAX_COLUMNS) -> None:
    count = len(list(columns))
    if count > max_columns:
        raise FileRejected(f"The file has {count} columns; at most {max_columns} can be imported.")
