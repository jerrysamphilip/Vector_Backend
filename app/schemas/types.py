"""Shared field types for request/response models.

``UtcDatetime``: a datetime that leaves the API in UTC with its zone (``2026-10-26T17:54:09.654321Z``). The
database holds naive UTC (DATETIME(6)), and a zone-less ISO string is read as *local* time by browsers, so
every datetime in a response model uses this type. Naive values are UTC; aware values are converted.
Validation (request parsing) is plain ``datetime``, unchanged, and the JSON schema stays
``{"type": "string", "format": "date-time"}`` so the OpenAPI document is identical. Python-mode
``model_dump()`` still returns the datetime; only JSON output is formatted.
"""
from datetime import datetime
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema

from app.core.clock import iso_utc

__all__ = ["UtcDatetime"]

UtcDatetime = Annotated[
    datetime,
    PlainSerializer(iso_utc, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
