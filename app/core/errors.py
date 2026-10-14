"""
Domain errors: how the service layer reports a failure the caller should see.

Services (app/services/*) raise these instead of fastapi.HTTPException, so business logic does not
depend on the web framework and can be reused from the worker, scripts and tests. One handler
(``domain_error_handler``, registered in app/main.py) turns them into HTTP responses with exactly
the body an HTTPException produces::

    {"detail": <message>}            # plain error
    {"detail": <structured payload>} # when ``detail`` is given (dict / list), e.g. per-field errors

Status codes (``status_code`` class attribute):

    BadRequestError             400   input the service rejects (bad value, wrong state for the action)
    PermissionDenied            403   the user may not do this to this record
    NotFoundError               404   the record does not exist or is outside the user's scope
    ConflictError               409   the request clashes with existing data or the record's state
    GoneError                   410   the record existed but can no longer be used (expired invitation)
    TooManyRequests             429   retry later; ``retry_after`` becomes the Retry-After header
    ValidationFailed            422   semantic validation failure (same code as FastAPI's own 422)
    ExternalServiceUnavailable  503   a dependency (mail provider, OpenAI, ...) cannot be reached
    OperationFailed             500   the operation failed server-side with a user-visible message

``code`` is a stable machine-readable name for logs and tests; it is not part of the response body
(the body shape is kept identical to the HTTPException responses these errors replaced).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base class. ``message`` is shown to the user unless a structured ``detail`` is given."""

    status_code: int = 400
    default_code: str = "DOMAIN_ERROR"

    def __init__(self, message: Any = "", *, code: Optional[str] = None, detail: Any = None) -> None:
        # A structured payload may also be passed positionally: NotFoundError({"field": "..."})
        if not isinstance(message, str):
            detail, message = message, str(message)
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self._detail = detail

    @property
    def detail(self) -> Any:
        """What goes into the response's "detail" field: the structured payload, else the message."""
        return self._detail if self._detail is not None else self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code}, code={self.code!r}, message={self.message!r})"


class BadRequestError(DomainError):
    status_code = 400
    default_code = "BAD_REQUEST"


BadRequest = BadRequestError


class PermissionDenied(DomainError):
    status_code = 403
    default_code = "PERMISSION_DENIED"


class NotFoundError(DomainError):
    status_code = 404
    default_code = "NOT_FOUND"


class ConflictError(DomainError):
    status_code = 409
    default_code = "CONFLICT"


class GoneError(DomainError):
    """The record existed but can no longer be used (410), e.g. an expired invitation."""
    status_code = 410
    default_code = "GONE"


class TooManyRequests(DomainError):
    """Retry later (429). ``retry_after`` (seconds) is sent as the Retry-After header."""
    status_code = 429
    default_code = "TOO_MANY_REQUESTS"

    def __init__(self, message: str, *, retry_after: Optional[int] = None, code: Optional[str] = None,
                 detail: Any = None) -> None:
        super().__init__(message, code=code, detail=detail)
        self.retry_after = retry_after


class ValidationFailed(DomainError):
    status_code = 422
    default_code = "VALIDATION_FAILED"


class ExternalServiceUnavailable(DomainError):
    status_code = 503
    default_code = "EXTERNAL_SERVICE_UNAVAILABLE"


class OperationFailed(DomainError):
    """The operation itself failed server-side (500) with a message the API has always shown,
    e.g. "AI generation failed: ..." after an LLM call blew up."""
    status_code = 500
    default_code = "OPERATION_FAILED"


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI exception handler: DomainError -> {"detail": ...} with the class's status code."""
    assert isinstance(exc, DomainError)
    level = logging.WARNING if exc.status_code >= 500 else logging.INFO
    logger.log(level, "%d %s on %s %s: %s", exc.status_code, exc.code, request.method, request.url.path, exc.message)
    retry_after = getattr(exc, "retry_after", None)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)


def register_domain_error_handler(app: FastAPI) -> None:
    """Install the one handler for every DomainError subclass (handlers are looked up by MRO)."""
    app.add_exception_handler(DomainError, domain_error_handler)
