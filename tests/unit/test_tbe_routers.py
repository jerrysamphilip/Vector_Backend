"""Route layer of the tbe items: the custom property routes (C-17: include_inactive, DELETE = deactivate,
{value, label} options in the body) for contacts and companies, and GET dns-records ``live`` (S-25). Fake
services through dependency_overrides; no database."""
import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError, register_domain_error_handler
from app.routers import accounts_router, contacts_router, deliverability_router
from app.schemas.contact_schema import FieldOption

ME = SimpleNamespace(user_id="me", tenant_id="t1", role="ADMIN")


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], ({"ok": True} if result is None else result), error

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


@pytest.fixture(params=[("/contacts", contacts_router, "field_service"),
                        ("/accounts", accounts_router, "company_field_service")])
def routes(request):
    prefix, module, provider = request.param
    fake = Recorder([])
    app = FastAPI()
    register_domain_error_handler(app)
    app.include_router(module.router)
    app.dependency_overrides[module.tenant_user] = lambda: ME
    app.dependency_overrides[getattr(module, provider)] = lambda: fake
    return prefix, TestClient(app), fake


def test_tbe_list_fields_passes_include_inactive(routes):
    prefix, client, fake = routes
    assert client.get(f"{prefix}/fields").status_code == 200
    assert client.get(f"{prefix}/fields", params={"include_inactive": "true"}).status_code == 200
    assert [c[1] for c in fake.calls] == [(ME, False), (ME, True)]


def test_tbe_field_body_takes_value_label_options_and_flags(routes):
    prefix, client, fake = routes
    fake.result = {"field_id": "f1"}
    r = client.patch(f"{prefix}/fields/f1", json={"label": "Tier", "options": ["Gold", {"value": "s", "label": "Silver"}],
                                                  "is_active": False, "max_length": 20})
    assert r.status_code == 200
    name, (user, field_id, payload), _ = fake.calls[0]
    assert name == "update" and field_id == "f1" and payload.field_type is None
    assert payload.options == ["Gold", FieldOption(value="s", label="Silver")]
    assert payload.is_active is False and payload.max_length == 20
    assert client.patch(f"{prefix}/fields/f1", json={"label": "Tier", "max_length": 0}).status_code == 422


def test_tbe_delete_field_deactivates_and_maps_rule_errors(routes):
    prefix, client, fake = routes
    fake.result = {"status": "deactivated", "field_id": "f1"}
    assert client.delete(f"{prefix}/fields/f1").json() == {"status": "deactivated", "field_id": "f1"}
    fake.error = BadRequestError("System properties cannot be deactivated")
    r = client.delete(f"{prefix}/fields/f1")
    assert r.status_code == 400 and "System properties" in r.text


def test_tbe_dns_records_route_passes_live_flag():
    fake = Recorder({"domain_name": "acme.io", "dns_records": []})
    out = deliverability_router.get_domain_dns_records("acme.io", False, fake, ME)
    assert out["domain_name"] == "acme.io" and fake.calls == [("dns_records", (ME, "acme.io", False), {})]
    params = inspect.signature(deliverability_router.get_domain_dns_records).parameters
    assert params["live"].default.default is True  # live comparison unless ?live=false
