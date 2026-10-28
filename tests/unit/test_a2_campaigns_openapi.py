"""
A2 refactor guard: the OpenAPI of the campaign, wizard, draft and template paths that existed before the
layering (tests/unit/snapshots/a2_campaigns_openapi.json, taken from the classic routers) is unchanged.
Documented additions of F-04 (edit after launch): four new paths and their schemas, ``wait_business_days``
on CampaignCreate / CampaignUpdate / CampaignResponse and ``pacing_warning`` on CampaignStateChange.
"""
import copy
import importlib
import json
from pathlib import Path

from fastapi import FastAPI

SNAPSHOT = Path(__file__).with_name("snapshots") / "a2_campaigns_openapi.json"
ROUTERS = ("campaign_router", "campaign_wizard_router", "campaign_draft_router", "template_router")
NEW_PATHS = {"/campaigns/{campaign_id}/edits", "/campaigns/{campaign_id}/edits/checks",
             "/campaigns/{campaign_id}/email-previews", "/campaigns/{campaign_id}/template-assignments"}
ADDED_FIELDS = {"CampaignCreate": {"wait_business_days"}, "CampaignUpdate": {"wait_business_days"},
                "CampaignResponse": {"wait_business_days"}, "CampaignStateChange": {"pacing_warning"}}


def _spec() -> dict:
    app = FastAPI()
    for name in ROUTERS:
        app.include_router(importlib.import_module(f"app.routers.{name}").router)
    return json.loads(json.dumps(app.openapi(), sort_keys=True, default=str))


def test_existing_paths_unchanged():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    spec = _spec()
    for path, methods in expected["paths"].items():
        for method, operation in methods.items():
            assert spec["paths"][path][method] == operation, f"{method.upper()} {path} changed"


def test_new_paths_are_the_documented_ones():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(_spec()["paths"]) - set(expected["paths"]) == NEW_PATHS


def test_schemas_only_gain_the_documented_fields():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["schemas"]
    schemas = _spec()["components"]["schemas"]
    for name, schema in expected.items():
        actual = copy.deepcopy(schemas[name])
        for added in ADDED_FIELDS.get(name, ()):
            assert added in actual["properties"], (name, added)
            del actual["properties"][added]
            assert added not in actual.get("required", [])
        assert actual == schema, name
