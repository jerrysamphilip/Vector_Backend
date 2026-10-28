# app/utils/excel_reader.py
"""
Prospect upload reader (POST /prospect-lists/.../upload): an uploaded CSV / XLSX as a DataFrame.

Uses the same file guards as the CRM import (app/utils/upload_guards.py): CSV text is UTF-8 or
Windows-1252 (never silently replaced), an XLSX is size-checked as a zip before pandas unpacks it
(zip bomb), and the column count is capped.
"""

import io

import pandas as pd
from fastapi import HTTPException, UploadFile

from app.utils.upload_guards import FileRejected, check_column_count, check_xlsx_archive, decode_csv

MAX_ROWS = 20000
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit
ALLOWED_CONTENT_TYPES = [
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
]


def _frame(content: bytes, filename: str) -> pd.DataFrame:
    if filename.endswith(".csv"):
        text = decode_csv(content)
        return pd.read_csv(io.StringIO(text), nrows=MAX_ROWS + 1)
    if filename.endswith(".xlsx"):
        check_xlsx_archive(content)
        return pd.read_excel(io.BytesIO(content), nrows=MAX_ROWS + 1)
    raise ValueError("Unsupported file type")


# Rejects files over MAX_FILE_SIZE or with a content type outside ALLOWED_CONTENT_TYPES (400)
def read_upload_file(file: UploadFile) -> pd.DataFrame:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file format")

    content = file.file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File size exceeds 5MB limit")

    try:
        df = _frame(content, file.filename or "")
        check_column_count(df.columns)
    except FileRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if len(df) > MAX_ROWS:
        raise HTTPException(status_code=413, detail=f"Too many rows: the limit is {MAX_ROWS:,} per upload")
    return df
