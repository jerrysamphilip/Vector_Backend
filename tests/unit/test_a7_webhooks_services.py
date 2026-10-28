"""A7 webhooks: delivery query object and WebhookService with a fake repository (no database, no network)."""
from types import SimpleNamespace

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.domain.webhooks import DeliveryFilter, WebhookInputError, clean_description
from app.services import webhooks as svc
from app.utils import webhooks as wh

ME = SimpleNamespace(user_id="me", tenant_id="t1", email="me@x.example")


def test_delivery_filter():
    f = DeliveryFilter.parse("failed", "", 10, 20)
    assert (f.status, f.event_type, f.limit, f.offset) == ("FAILED", None, 10, 20)
    with pytest.raises(WebhookInputError):
        DeliveryFilter.parse("lost", None, 10, 0)
    assert clean_description("  hi ") == "hi" and clean_description("   ") is None


class FakeRepo:
    def __init__(self):
        self.eps = {}
        self.deliveries_by_id = {}
        self.added = []
        self.criteria = None

    def count_endpoints(self, tenant_id):
        return len(self.eps)

    def endpoint(self, tenant_id, endpoint_id):
        ep = self.eps.get(endpoint_id)
        return ep if ep is not None and ep.tenant_id == tenant_id else None

    def delivery(self, tenant_id, endpoint_id, delivery_id):
        return self.deliveries_by_id.get(delivery_id)

    def deliveries(self, tenant_id, endpoint_id, criteria):
        self.criteria = criteria
        return [], 0

    def add(self, entity):
        self.added.append(entity)
        if hasattr(entity, "endpoint_id") and type(entity).__name__ == "WebhookEndpoint":
            self.eps[entity.endpoint_id] = entity
        return entity

    def refresh(self, entity):
        pass


class FakeUow:
    def __init__(self):
        self.webhooks = FakeRepo()
        self.session = SimpleNamespace(info={})
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def commit(self):
        self.commits += 1


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(svc, "allow_insecure", lambda: False)
    monkeypatch.setattr(wh, "socket_resolver", lambda host, port: ["93.184.216.34"])
    return svc.WebhookService(FakeUow())


def test_create_returns_the_secret_once(service):
    body = service.create(ME, " https://hooks.example.com/x ", "  CRM ", ["contact.created"], True)
    assert body["secret"].startswith(wh.SECRET_PREFIX) and body["description"] == "CRM"
    assert body["url"] == "https://hooks.example.com/x" and service.uow.commits == 1
    assert "secret" not in service.get(ME, body["endpoint_id"])


def test_create_validates_url_types_and_limit(service):
    with pytest.raises(ValidationFailed):
        service.create(ME, "http://hooks.example.com/x", None, ["contact.created"], True)
    with pytest.raises(ValidationFailed):
        service.create(ME, "https://hooks.example.com/x", None, ["contact.exploded"], True)
    for i in range(wh.MAX_ENDPOINTS_PER_TENANT):
        service.uow.webhooks.eps[f"e{i}"] = SimpleNamespace(tenant_id="t1")
    with pytest.raises(ConflictError):
        service.create(ME, "https://hooks.example.com/x", None, ["contact.created"], True)


def test_deliveries_and_redeliver_guards(service):
    with pytest.raises(NotFoundError):
        service.deliveries(ME, "missing", None, None, 50, 0)
    with pytest.raises(ValidationFailed):
        service.deliveries(ME, "missing", "lost", None, 50, 0)
    ep = SimpleNamespace(endpoint_id="e1", tenant_id="t1", active=False)
    service.uow.webhooks.eps["e1"] = ep
    service.deliveries(ME, "e1", "pending", None, 5, 0)
    assert service.uow.webhooks.criteria.status == "PENDING"
    service.uow.webhooks.deliveries_by_id["d1"] = SimpleNamespace(status="FAILED", event_id="ev", event_type="contact.created",
                                                                  payload="{}", delivery_id="d1")
    with pytest.raises(ConflictError, match="Turn the endpoint on"):
        service.redeliver(ME, "e1", "d1")
    ep.active = True
    service.uow.webhooks.deliveries_by_id["d1"].status = "PENDING"
    with pytest.raises(ConflictError, match="still being retried"):
        service.redeliver(ME, "e1", "d1")
    service.uow.webhooks.deliveries_by_id["d1"].status = "FAILED"
    copy = service.redeliver(ME, "e1", "d1")
    assert copy["redelivery_of"] == "d1" and copy["status"] == "PENDING"
    other = SimpleNamespace(**{**vars(ME), "tenant_id": "t2"})
    with pytest.raises(NotFoundError):
        service.redeliver(other, "e1", "d1")
