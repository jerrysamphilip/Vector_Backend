"""Domain errors (app/core/errors.py) map to the same status codes and {"detail": ...} bodies as HTTPException."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.errors import (BadRequestError, ConflictError, DomainError, ExternalServiceUnavailable, NotFoundError,
                             PermissionDenied, ValidationFailed, register_domain_error_handler)
from app.utils.error_utils import handle_route_error

CASES = [
    (BadRequestError, 400, "BAD_REQUEST"),
    (PermissionDenied, 403, "PERMISSION_DENIED"),
    (NotFoundError, 404, "NOT_FOUND"),
    (ConflictError, 409, "CONFLICT"),
    (ValidationFailed, 422, "VALIDATION_FAILED"),
    (ExternalServiceUnavailable, 503, "EXTERNAL_SERVICE_UNAVAILABLE"),
]


def _client(exc: Exception) -> TestClient:
    app = FastAPI()
    register_domain_error_handler(app)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("cls,status,code", CASES)
def test_de_u01_status_code_and_default_code(cls, status, code):
    err = cls("Something went wrong")
    assert isinstance(err, DomainError)
    assert err.status_code == status
    assert err.code == code
    assert err.message == "Something went wrong" == str(err)
    assert err.detail == "Something went wrong"


@pytest.mark.parametrize("cls,status,code", CASES)
def test_de_u02_response_matches_http_exception(cls, status, code):
    domain = _client(cls("Lead not found")).get("/boom")
    http = _client(HTTPException(status_code=status, detail="Lead not found")).get("/boom")
    assert domain.status_code == http.status_code == status
    assert domain.json() == http.json() == {"detail": "Lead not found"}


def test_de_u03_structured_detail_payload():
    payload = {"message": "Fix these rows", "errors": [{"row": 2, "field": "email"}]}
    res = _client(ConflictError("Fix these rows", detail=payload)).get("/boom")
    assert res.status_code == 409
    assert res.json() == {"detail": payload}
    # A payload passed positionally is treated the same way
    assert NotFoundError(payload).detail == payload


def test_de_u04_custom_code_is_not_in_the_body():
    err = BadRequestError("Amount must be a number", code="AMOUNT_NOT_NUMBER")
    assert err.code == "AMOUNT_NOT_NUMBER"
    assert _client(err).get("/boom").json() == {"detail": "Amount must be a number"}


def test_de_u05_handle_route_error_reraises_domain_errors_unchanged():
    err = ConflictError("duplicate")
    with pytest.raises(ConflictError) as exc:
        try:
            raise err
        except Exception as e:
            handle_route_error(e, context="test")
    assert exc.value is err


def test_de_u06_app_registers_the_handler():
    from app.main import app
    assert app.exception_handlers.get(DomainError) is not None
