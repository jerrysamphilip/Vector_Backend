# app/utils/excel_reader.py

import pandas as pd
import io
from fastapi import UploadFile, HTTPException

MAX_ROWS = 20000
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit
ALLOWED_CONTENT_TYPES = [
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
]

# Rejects files over MAX_FILE_SIZE or with a content type outside ALLOWED_CONTENT_TYPES (400)
def read_upload_file(file: UploadFile) -> pd.DataFrame:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file format")
        
    content = file.file.read()
    
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File size exceeds 5MB limit")

    try:
        filename = file.filename or ""
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), nrows=MAX_ROWS + 1)
        elif filename.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(content), nrows=MAX_ROWS + 1)
        else:
            raise ValueError("Unsupported file type")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if len(df) > MAX_ROWS:
        raise HTTPException(status_code=413, detail=f"Too many rows: the limit is {MAX_ROWS:,} per upload")
    return df
