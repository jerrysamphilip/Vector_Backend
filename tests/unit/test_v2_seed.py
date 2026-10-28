"""Chunk V2: the load-data seeder's safety guard and deterministic generators (no database)."""
import importlib.util
import random
import sys
from datetime import datetime
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts" / "load" / "seed_load_data.py"
_spec = importlib.util.spec_from_file_location("v2_seed_load_data", PATH)
seed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seed
_spec.loader.exec_module(seed)

NOW = datetime(2026, 10, 27, 12, 0, 0)
MIGRATED = ["alembic_version", "prospects", "tenants", "users"]


@pytest.mark.parametrize("name", ["outreach_ai", "vtest_v2", "load", "prod_load_1", "vtest_load-x", ""])
def test_guard_refuses_non_load_databases(name):
    assert seed.check_target(name, MIGRATED, {}) is not None


@pytest.mark.parametrize("name", ["vtest_load", "vtest_load_full", "load_2026"])
def test_guard_accepts_empty_migrated_load_databases(name):
    assert seed.check_target(name, MIGRATED, {"tenants": 0, "prospects": 0}) is None


def test_guard_refuses_unmigrated_or_non_empty():
    assert "migrate" in seed.check_target("vtest_load", ["tenants"], {})
    assert "not empty" in seed.check_target("vtest_load", MIGRATED, {"tenants": 1, "prospects": 0})


def _world(contacts=2000, accounts=60):
    rng = random.Random(5)
    world = seed.World(tenant_id=seed.new_id(rng))
    world.users = seed.gen_users(rng, world, "hash", NOW)
    world.inboxes = ["i1", "i2", "i3"]
    world.accounts = seed.gen_accounts(rng, world, accounts, NOW)
    world.contacts = seed.gen_contacts(rng, world, contacts, 0.05, NOW)
    return rng, world


def test_users_cover_roles_and_two_teams():
    _, world = _world(contacts=10)
    roles = {u["role"] for u in world.users}
    assert len(world.users) == 10 and roles == {"SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT"}
    by_id = {u["user_id"]: u for u in world.users}
    for agent in (u for u in world.users if u["role"] == "AGENT"):
        assert by_id[agent["manager_id"]]["_team"] == agent["_team"]
    assert len({u["email"] for u in world.users}) == 10


def test_accounts_have_unique_names_and_domains():
    _, world = _world(contacts=10, accounts=500)
    assert len({a["name"] for a in world.accounts}) == 500
    assert len({a["domain"] for a in world.accounts}) == 500


def test_contacts_unique_emails_and_about_five_percent_duplicates():
    _, world = _world()
    emails = [c["email"] for c in world.contacts]
    assert len(set(emails)) == len(emails) == 2000
    keys = {}
    for c in world.contacts:
        if c["account_id"]:
            keys.setdefault((c["first_name"], c["last_name"], c["account_id"]), []).append(c)
    dupes = sum(len(v) - 1 for v in keys.values() if len(v) > 1)
    assert 0.02 <= dupes / len(world.contacts) <= 0.10
    assert all(c["custom_fields"]["region"] in seed.REGIONS for c in world.contacts)


def test_generation_is_deterministic():
    _, a = _world(contacts=300)
    _, b = _world(contacts=300)
    assert [c["email"] for c in a.contacts] == [c["email"] for c in b.contacts]
    assert [c["prospect_id"] for c in a.contacts] == [c["prospect_id"] for c in b.contacts]


def test_campaign_messages_respect_unique_keys():
    rng, world = _world(contacts=1500)
    out = seed.gen_campaigns(rng, world, 8, 3000, NOW)
    outbound = [(m["campaign_id"], m["prospect_id"], m["sequence_id"]) for m in out.messages
                if m["direction"] == "OUTBOUND" and m["status"] != "CANCELLED"]
    assert len(outbound) == len(set(outbound))                       # ux_email_messages_step_key
    pairs = [(c["prospect_id"], c["inbox_id"]) for c in out.conversations]
    assert len(pairs) == len(set(pairs))                             # uq_conversations_prospect_inbox
    enrol = [(e["campaign_id"], e["prospect_id"]) for e in out.enrolments]
    assert len(enrol) == len(set(enrol))                             # uq_campaign_prospect
    assert all(1 <= e["current_step"] <= 7 for e in out.enrolments)  # ck_step_range
    assert 2500 <= len(out.messages) <= 3600
    assert {m["status"] for m in out.messages} >= {"SENT", "QUEUED"}


def test_rows_normalise_to_one_key_set():
    rows = seed.normalise_keys(seed.strip_private([{"a": 1, "_x": 2}, {"b": 3}]))
    assert rows == [{"a": 1, "b": None}, {"a": None, "b": 3}]
