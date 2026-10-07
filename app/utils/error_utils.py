# app/utils/error_utils.py
"""
Standardized error handling for FastAPI route handlers.

Usage:
    from app.utils.error_utils import handle_route_error

    @router.get("/{id}")
    async def get_something(id: str):
        try:
            # ... business logic
        except Exception as e:
            handle_route_error(e, context="get_something")
"""

import logging
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Exception → HTTP status mapping
# ──────────────────────────────────────────────────────────────────────────────

# Map well-known exception types to (http_status, safe_client_message)
_EXCEPTION_MAP: list[tuple[type, int, str]] = [
    # Re-raise FastAPI errors unchanged
    (HTTPException,          0,   ""),

    # Business-logic errors → 400
    (ValueError,             400, None),   # None = use str(e)

    # Not-found style errors → 404
    (KeyError,               404, None),
    (LookupError,            404, None),

    # Permissions → 403
    (PermissionError,        403, None),

    # DB constraint violations → 409 Conflict
    (IntegrityError,         409, "A database constraint was violated."),

    # DB connection / timeout → 503
    (OperationalError,       503, "Database unavailable. Please try again."),

    # All other exceptions → 500 (never expose internal details)
    (Exception,              500, "An internal server error occurred."),
]


def handle_route_error(e: Exception, context: str = "") -> None:
    """
    Convert *e* into an HTTPException with the correct status code and log
    it appropriately.  Always raises — never returns normally.

    Args:
        e:       The caught exception.
        context: A short label for log messages (e.g. "create_campaign").

    Raises:
        HTTPException: Always.
    """
    prefix = f"[{context}] " if context else ""

    # HTTPException: re-raise exactly as-is (already formatted for the client)
    if isinstance(e, HTTPException):
        raise

    for exc_type, status_code, safe_message in _EXCEPTION_MAP:
        if isinstance(e, exc_type):
            if status_code == 0:        # HTTPException sentinel — re-raise
                raise

            # Choose between using the exception message or a safe generic one
            client_detail = safe_message if safe_message else str(e)

            # Log severity by status code
            if status_code >= 500:
                logger.error("%s%s: %s", prefix, type(e).__name__, e, exc_info=True)
            elif status_code >= 400:
                logger.warning("%s%s: %s", prefix, type(e).__name__, e)

            raise HTTPException(status_code=status_code, detail=client_detail)

    # Fallback (should not be reached given `Exception` catch-all above)
    logger.error("%sUnhandled exception %s: %s", prefix, type(e).__name__, e, exc_info=True)
    raise HTTPException(status_code=500, detail="An internal server error occurred.")
