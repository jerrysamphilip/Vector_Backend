# app/utils/excel_reader.py

import pandas as pd
import io
from fastapi import UploadFile, HTTPException

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit
ALLOWED_CONTENT_TYPES = [
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
]

# Kartik has changed this: Added strict file size and type validation parameters
def read_upload_file(file: UploadFile) -> pd.DataFrame:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file format")
        
    content = file.file.read()
    
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File size exceeds 5MB limit")

    try:
        if file.filename.endswith(".csv"):
            return pd.read_csv(io.BytesIO(content))
        elif file.filename.endswith(".xlsx"):
            return pd.read_excel(io.BytesIO(content))
        else:
            raise ValueError("Unsupported file type")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
