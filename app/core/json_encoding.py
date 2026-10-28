"""How a bare ``datetime`` in a route's return value is written to JSON (UAT defect 2).

Response models use ``app.schemas.types.UtcDatetime``. Routes that return plain dicts go through FastAPI's
``jsonable_encoder``, which writes ``datetime.isoformat()``: for the naive UTC values the database holds that
is a string without a zone, which browsers read as local time. ``install_utc_datetime_encoder()`` (called
once by app.main) makes it write ``iso_utc()`` instead (``...Z``). Dates and times are unchanged.
"""
from datetime import datetime

from fastapi import encoders

from app.core.clock import iso_utc

__all__ = ["install_utc_datetime_encoder"]


def install_utc_datetime_encoder() -> None:
    """Idempotent. jsonable_encoder looks a value's exact type up in ENCODERS_BY_TYPE at call time."""
    encoders.ENCODERS_BY_TYPE[datetime] = iso_utc
