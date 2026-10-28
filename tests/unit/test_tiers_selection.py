"""scripts/tierlib.py: case-id patterns, tier resolution, parallel safety, plan steps and estimates."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tierlib", ROOT / "scripts" / "tierlib.py")
tl = importlib.util.module_from_spec(_spec)
sys.modules["tierlib"] = tl
_spec.loader.exec_module(tl)

SUITES = [
    {"id": "unit", "layer": "unit", "kind": "pytest"},
    {"id": "contacts", "layer": "system"},
    {"id": "api_sweep", "layer": "system"},
    {"id": "e2e", "layer": "e2e"},
    {"id": "ui_outreach", "layer": "ui"},
    {"id": "ui_features.a", "layer": "ui"},
    {"id": "ui_features.platform_admin", "layer": "ui"},
    {"id": "reports_platform", "layer": "system", "run_last": True},
]


def _data(**over):
    data = {
        "locks": {"worker": "shared worker"},
        "suites": {
            "unit": {"parallel": True, "reason": "pure"},
            "contacts": {"parallel": False, "reason": "global purge"},
            "e2e": {"parallel": True, "locks": ["worker"], "reason": "jobs"},
            "ui_features.*": {"parallel": True, "reason": "own tenant"},
            "ui_features.platform_admin": {"parallel": False, "reason": "scheduler"},
        },
        "tiers": {
            "smoke": {"suites": {
                "unit": {"reason": "fast"},
                "e2e": {"cases": ["E2E-J1-*"], "reason": "J1"},
                "ui_outreach": {"cases": ["UIO-AUTH-01"], "reason": "login"},
                "ui_features.platform_admin": {"cases": ["UIF-PA-01"], "parallel": True, "reason": "no scheduler case"},
            }},
            "core": {"include": ["smoke"], "suites": {
                "layer:system": {"reason": "all system"},
                "e2e": {"reason": "all journeys"},
                "ui_outreach": {"cases": ["UIO-SHELL-*"], "reason": "shell"},
                "ui_features.*": {"cases": ["UIF-*-01"], "reason": "first case"},
            }},
            "full": {"all_suites": True, "reason": "everything"},
        },
    }
    data.update(over)
    return data


# ── patterns ──

@pytest.mark.parametrize("pattern,cid,expected", [
    ("UIO-AUTH-3?", "UIO-AUTH-31", True),
    ("UIO-AUTH-3?", "UIO-AUTH-3", False),
    ("UIC-CON-2[3-5]", "UIC-CON-24", True),
    ("UIC-CON-2[3-5]", "UIC-CON-26", False),
    ("API-*-ISO-*", "API-GET-contacts-ISO-path-foreign-id", True),
    ("API-*-ISO-*", "API-GET-contacts-VAL-body", False),
    ("E2E-J1-*", "e2e-j1-01", False),  # case-sensitive
])
def test_matches(pattern, cid, expected):
    assert tl.matches(cid, [pattern]) is expected


@pytest.mark.parametrize("cid", ["UIC-CONTACTS-CRASH", "E2E-J2-EXC", "API-x-tail_phase-SETUP"])
def test_crash_and_setup_rows_are_always_kept(cid):
    assert tl.matches(cid, ["NOTHING-*"])


@pytest.mark.parametrize("pattern,prefix,expected", [
    ("UIO-AUTH-*", "UIO-AUTH-", True),
    ("UIO-AUTH-*", "UIO-SHELL-", False),
    ("*-ISO-*", "API-", True),
    ("UIO-A", "UIO-AUTH-", False),
    ("E2E-J[12]-*", "E2E-J2-", True),
    ("E2E-J[!1]-*", "E2E-J1-", False),
    ("UIO-?ET-01", "UIO-DET-", True),
])
def test_could_match_prefix(pattern, prefix, expected):
    assert tl.could_match_prefix(pattern, prefix) is expected


def test_patterns_from_spec():
    assert tl.patterns_from(" A-*, ,B-01 ") == ["A-*", "B-01"]
    assert tl.patterns_from("") == []


def test_split_and_filter_cases():
    ids = ["A-01", "A-02", "B-01"]
    assert tl.split_cases(ids, ["A-*"]) == (["A-01", "A-02"], ["B-01"])
    assert tl.split_cases(ids, None) == (ids, [])
    rows = [{"id": "A-01"}, {"id": "A-02", "carried_over": True}, {"id": "B-01"}]
    kept, dropped = tl.filter_cases(rows, ["A-*"])
    assert [r["id"] for r in kept] == ["A-01"] and len(dropped) == 2


# ── resolution ──

def test_smoke_cuts_and_whole_suites():
    sel = tl.resolve(_data(), "smoke", SUITES)
    assert set(sel) == {"unit", "e2e", "ui_outreach", "ui_features.platform_admin"}
    assert "cases" not in sel["unit"]
    assert sel["e2e"]["cases"] == ["E2E-J1-*"]


def test_core_includes_smoke_union_and_whole_wins():
    sel = tl.resolve(_data(), "core", SUITES)
    assert {"contacts", "api_sweep", "reports_platform"} <= set(sel)  # layer:system
    assert "cases" not in sel["e2e"]  # whole in core beats J1-only from smoke
    assert sel["ui_outreach"]["cases"] == ["UIO-AUTH-01", "UIO-SHELL-*"]
    # exact key beats the glob; own entry drops smoke's parallel override
    assert sel["ui_features.platform_admin"]["cases"] == ["UIF-PA-01", "UIF-*-01"]
    assert "parallel" not in sel["ui_features.platform_admin"]


def test_full_and_all_alias_run_everything_whole():
    for name in ("full", "all"):
        sel = tl.resolve(_data(), name, SUITES)
        assert set(sel) == {s["id"] for s in SUITES}
        assert all("cases" not in e for e in sel.values())


def test_unknown_suite_and_tier_raise():
    data = _data()
    data["tiers"]["smoke"]["suites"]["nope"] = {"reason": "typo"}
    with pytest.raises(tl.TierError, match="nope"):
        tl.resolve(data, "smoke", SUITES)
    with pytest.raises(tl.TierError, match="unknown tier"):
        tl.resolve(_data(), "nightly", SUITES)


def test_structure_problems():
    data = _data()
    data["tiers"]["smoke"]["suites"]["unit"] = {"cases": [], "bogus": 1}
    data["tiers"]["core"]["include"] = ["core"]
    data["suites"]["e2e"]["locks"] = ["undefined"]
    problems = " | ".join(tl.structure_problems(data))
    for needle in ("unknown key 'bogus'", "needs a one-line 'reason'", "non-empty list", "include cycle", "unknown lock"):
        assert needle in problems


def test_load_rejects_bad_json(tmp_path):
    f = tmp_path / "t.json"
    f.write_text("{nope")
    with pytest.raises(tl.TierError, match="not valid JSON"):
        tl.load(f)


# ── safety, steps, estimates ──

def test_safety_exact_glob_default_and_override():
    data = _data()
    assert tl.safety(data, "ui_features.a")["parallel"] is True  # glob
    assert tl.safety(data, "ui_features.platform_admin")["parallel"] is False  # exact beats glob
    assert tl.safety(data, "api_sweep")["parallel"] is False  # unclassified -> serial
    assert tl.safety(data, "e2e")["locks"] == ["worker"]
    smoke = tl.resolve(data, "smoke", SUITES)
    assert tl.safety(data, "ui_features.platform_admin", smoke["ui_features.platform_admin"])["parallel"] is True


def test_plan_steps_serial_and_pool():
    safe = {"unit": True, "contacts": False, "e2e": True, "reports_platform": False}
    suites = [s for s in SUITES if s["id"] in safe]
    one = tl.plan_steps(suites, lambda s: {"parallel": safe[s["id"]]}, 1)
    assert [st["suites"][0]["id"] for st in one] == ["unit", "contacts", "e2e", "reports_platform"]
    four = tl.plan_steps(suites, lambda s: {"parallel": safe[s["id"]]}, 4)
    assert [(st["parallel"], [s["id"] for s in st["suites"]]) for st in four] == [
        (True, ["unit", "e2e"]), (False, ["contacts"]), (False, ["reports_platform"])]


def test_simulate_respects_slots_and_locks():
    assert tl.simulate([(10, []), (10, []), (10, [])], 3) == 10
    assert tl.simulate([(10, []), (10, []), (10, [])], 2) == 20
    assert tl.simulate([(10, ["w"]), (10, ["w"]), (5, [])], 4) == 20


def test_estimate_scales_by_selected_share():
    timing = {"seconds": 100, "cases": [f"C-{i:02d}" for i in range(10)], "run": "r1"}
    secs, src = tl.estimate({"cases": ["C-0[0-1]"]}, timing, timing["cases"], 50)
    assert secs == pytest.approx(20) and "2/10" in src
    assert tl.estimate({}, timing, timing["cases"], 50)[0] == 100
    assert tl.estimate({"estimate_s": 7}, timing, timing["cases"], 50) == (7.0, "tiers file")
    assert tl.estimate({}, None, [], 50)[0] == 50  # registry fallback
    filtered = dict(timing, cases=timing["cases"][:2], filtered=True)  # time of a cut run: scale up to the whole suite
    assert tl.estimate({}, filtered, timing["cases"], 50)[0] == pytest.approx(500)


# ── the real TIERS.json ──

def test_repository_tiers_file_is_valid_and_nested():
    data = tl.load(ROOT / "system_tests" / "TIERS.json")
    assert set(tl.TIER_NAMES) <= set(data["tiers"])
    assert data["tiers"]["core"]["include"] == ["smoke"]
    assert data["tiers"]["full"].get("all_suites") is True
    raw = json.loads((ROOT / "system_tests" / "TIERS.json").read_text())
    for name, tier in raw["tiers"].items():
        for key, entry in (tier.get("suites") or {}).items():
            assert entry.get("reason"), f"{name}/{key} has no reason"
