"""
Optimistic concurrency for the main editable records (rule T-3).

Every guarded table (contacts, leads, opportunities, proposals, campaigns, sequence steps and
companies) has an integer ``row_version`` column (migration 0016_optimistic_concurrency), returned in
the record's API responses. A client that edits or changes the state of a record sends back the
version it loaded, either as ``expected_version`` in the JSON body or as an ``If-Match`` header
(``If-Match: 7`` or ``If-Match: "7"``). If someone else changed the record in between, the request is
refused with 409 instead of silently overwriting their change::

    {"detail": {"code": "STALE_VERSION", "current_version": 8,
                "message": "This record was changed by someone else. Reload to see the latest version."}}

How: ``claim_version`` runs ``UPDATE <table> SET row_version = row_version + 1 WHERE <pk> AND
row_version = <expected>`` at the start of the unit of work. The UPDATE is atomic and keeps the row
locked until the transaction ends, so two concurrent saves with the same version cannot both pass:
the second one matches no row and gets the 409. (A SQLAlchemy ``version_id_col`` was not used: it
bumps the version on every ORM flush, so background writers such as contact rescoring, IMAP sync or
the campaign scheduler would cause spurious conflicts for users and would themselves fail with
StaleDataError; the version here tracks user edits through the guarded endpoints only.)

Transition: a request without a version is still accepted (and bumps the version), with a
deprecation log line, because API scripts and the system test suites do not send one yet. The web
client always sends it. Set OCC_REQUIRE_VERSION=true to refuse such requests with 428.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, cast

from fastapi import Header
from sqlalchemy import CursorResult, and_, inspect, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import BadRequestError, ConflictError, DomainError, NotFoundError

logger = logging.getLogger(__name__)

VERSION_COLUMN = "row_version"
STALE_VERSION = "STALE_VERSION"
STALE_MESSAGE = "This record was changed by someone else. Reload to see the latest version."


class PreconditionRequired(DomainError):
    """428: the endpoint needs the version the client saw (only when OCC_REQUIRE_VERSION is on)."""
    status_code = 428
    default_code = "VERSION_REQUIRED"


def version_required() -> bool:
    return os.environ.get("OCC_REQUIRE_VERSION", "").strip().lower() in ("1", "true", "yes", "on")


def parse_if_match(value: Optional[str]) -> Optional[int]:
    """The version in an If-Match header: 7, "7" or W/"7". Empty or * means "no precondition"."""
    if value is None:
        return None
    v = value.strip()
    if not v or v == "*":
        return None
    if v[:2] in ("W/", "w/"):
        v = v[2:].strip()
    v = v.strip('"').strip()
    if not v.isdigit():
        raise BadRequestError("If-Match must be the record's row_version, e.g. If-Match: \"3\"",
                              code="BAD_IF_MATCH")
    return int(v)


def if_match_version(if_match: Optional[str] = Header(None, alias="If-Match")) -> Optional[int]:
    """FastAPI dependency: the version sent in the If-Match header, if any."""
    return parse_if_match(if_match)


def resolve_expected(body_value: Optional[int], header_value: Optional[int]) -> Optional[int]:
    """One expected version from the body field and the If-Match header; they must agree."""
    if body_value is not None and header_value is not None and body_value != header_value:
        raise BadRequestError("expected_version and If-Match disagree", code="BAD_IF_MATCH")
    return body_value if body_value is not None else header_value


def stale_error(current: Optional[int]) -> ConflictError:
    return ConflictError(STALE_MESSAGE, code=STALE_VERSION,
                         detail={"code": STALE_VERSION, "current_version": current, "message": STALE_MESSAGE})


def claim_version(db: Session, obj: Any, expected: Optional[int], *, record: str = "record") -> int:
    """Check the version the client saw and bump it, atomically; returns the new version.

    Call it right after loading the record and before changing it, inside the request's unit of
    work: a later error rolls the bump back with everything else. ``expected=None`` (client sent no
    version) skips the check but still bumps, so other clients see the change.
    """
    mapper = inspect(obj).mapper
    table = mapper.local_table
    pk_values = mapper.primary_key_from_instance(obj)
    where = and_(*(col == val for col, val in zip(mapper.primary_key, pk_values)))
    version_col = table.c[VERSION_COLUMN]

    stmt = update(table).where(where)
    if expected is None:
        if version_required():
            raise PreconditionRequired(
                "Send the version you loaded (expected_version or If-Match).",
                detail={"code": "VERSION_REQUIRED", "message": "Send the version you loaded (expected_version or If-Match)."})
        logger.info("Deprecated: %s %s updated without expected_version / If-Match (T-3)", record, pk_values)
    else:
        stmt = stmt.where(version_col == expected)
    result = cast(CursorResult[Any], db.execute(stmt.values({VERSION_COLUMN: version_col + 1})))
    # Locking read: sees the latest committed version, not this transaction's REPEATABLE READ snapshot
    current = db.execute(select(version_col).where(where).with_for_update()).scalar()
    if current is None:
        raise NotFoundError(f"{record.capitalize()} not found")
    if result.rowcount != 1:
        raise stale_error(int(current))
    set_committed_value(obj, VERSION_COLUMN, current)
    return int(current)
