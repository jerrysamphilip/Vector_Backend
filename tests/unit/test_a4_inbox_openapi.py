"""A4 refactor guard: the OpenAPI of the inbox, conversation and deliverability routers (operations and the
schemas they reference) is identical to the snapshot taken from the classic routers before the layering
(snapshots/a4_inbox_openapi.json)."""
import json
from pathlib import Path

from fastapi import FastAPI

from app.routers import conversation_router, deliverability_router, inbox_router

SNAPSHOT = Path(__file__).with_name("snapshots") / "a4_inbox_openapi.json"


def spec() -> dict:
    app = FastAPI()
    for module in (inbox_router, conversation_router, deliverability_router):
        app.include_router(module.router)
    return json.loads(json.dumps(app.openapi(), sort_keys=True, default=str))


def test_inbox_conversation_deliverability_openapi_unchanged():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = spec()
    assert sorted(actual["paths"]) == sorted(expected["paths"])
    for path, methods in expected["paths"].items():
        assert actual["paths"][path] == methods, f"{path} changed"
    assert actual.get("components") == expected.get("components")
