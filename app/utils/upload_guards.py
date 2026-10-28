# app/utils/upload_guards.py
"""
Guards for spreadsheet files people upload (CRM import, prospect upload). Pure: bytes in, text or
FileRejected out; no database, no web framework.

- decode_csv: UTF-8 (with or without BOM), else Windows-1252 (Excel's "CSV" on Windows);
  bytes that are neither are refused instead of being replaced silently.
- check_xlsx_archive / check_column_count: an XLSX is a zip; a small file can expand to
  gigabytes (zip bomb) or hold thousands of columns, so both are checked before it is parsed.
- check_size: the upload itself.

app/services/import_limits.py re-exports these for the service layer.
"""

import io
import zipfile
from typing import Any, Iterable

MAX_COLUMNS = 200
MAX_XLSX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


class FileRejected(ValueError):
    """The file cannot be imported; the message is safe to show to the user."""


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


def check_size(raw: bytes, max_bytes: int) -> None:
    if len(raw) > max_bytes:
        raise FileRejected(f"File is larger than {max_bytes // (1024 * 1024)} MB")
