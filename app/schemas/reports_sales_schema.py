"""Request bodies of the sales reports (POST /sales-reports/forecast-snapshots)."""
from typing import Optional

from pydantic import BaseModel, Field


class SnapshotRequest(BaseModel):
    period_type: Optional[str] = Field(None, pattern="^(MONTH|QUARTER)$")  # none = both
    replace: bool = False              # retake the current period's snapshot
