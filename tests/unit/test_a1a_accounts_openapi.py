"""
A1 refactor guard: the OpenAPI of the account paths that existed before the layering (snapshot taken from the
classic router, tests/unit/snapshots/a1a_accounts_openapi.json) is unchanged, except for the documented
additions of this round: GET /accounts gains the ``filters`` and ``status`` query parameters (C-16 company
property filters, F-14 Active / Archived) and the request bodies gain ``custom_fields`` (C-16).
"""
import copy
import json
from pathlib import Path

from fastapi import FastAPI

from app.routers.accounts_router import router

SNAPSHOT = Path(__file__).with_name("snapshots") / "a1a_accounts_openapi.json"
ADDED_LIST_PARAMS = {"filters", "status"}
NEW_PATHS = {"/accounts/meta", "/accounts/export", "/accounts/fields", "/accounts/fields/{field_id}",
             "/accounts/backfill/previews", "/accounts/backfill/runs", "/accounts/backfill/runs/{run_id}/undo"}


def _spec() -> dict:
    app = FastAPI()
    app.include_router(router)
    return json.loads(json.dumps(app.openapi(), sort_keys=True, default=str))


def test_existing_account_paths_unchanged_apart_from_documented_additions():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    spec = _spec()
    for path, methods in expected["paths"].items():
        for method, operation in methods.items():
            actual = copy.deepcopy(spec["paths"][path][method])
            if (path, method) == ("/accounts", "get"):
                added = [p for p in actual["parameters"] if p["name"] in ADDED_LIST_PARAMS]
                assert {p["name"] for p in added} == ADDED_LIST_PARAMS
                assert all(not p.get("required") for p in added)
                actual["parameters"] = [p for p in actual["parameters"] if p["name"] not in ADDED_LIST_PARAMS]
            assert actual == operation, f"{method.upper()} {path} changed"


def test_new_paths_are_the_documented_ones():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(_spec()["paths"]) - set(expected["paths"]) == NEW_PATHS


def test_account_bodies_only_gain_custom_fields():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["schemas"]
    schemas = _spec()["components"]["schemas"]
    for name, schema in expected.items():
        actual = copy.deepcopy(schemas[name])
        assert "custom_fields" in actual["properties"]
        del actual["properties"]["custom_fields"]
        assert actual == schema, name
