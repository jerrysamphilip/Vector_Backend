"""Request bodies of the custom report builder (POST /reports/custom/run, POST / PATCH /reports/custom)."""
from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class Definition(BaseModel):
    object: str
    group_by: str
    group_by_2: Optional[str] = None
    measure: str = "count"
    filters: List[Dict[str, Any]] = []
    date_field: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    chart: str = "bar"           # bar / table


class ReportWrite(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[Definition] = None
    shared: Optional[bool] = None
