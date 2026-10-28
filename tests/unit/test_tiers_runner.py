"""scripts/test.py tier plumbing: TEST_CASES env, result filtering, timings / catalogues, summary section, CLI."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_runner_script", ROOT / "scripts" / "test.py")
runner = importlib.util.module_from_spec(_spec)
sys.modules["test_runner_script"] = runner
_spec.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def _no_stack(monkeypatch):
    # never probe docker / the API from a unit test
    monkeypatch.setitem(runner._RELAXED, "m", 1)


def test_tier_env_sets_and_clears_test_cases():
    suite = {"_tier": {"cases": ["A-*", "B-01"], "env": {"AREAS": "x"}}}
    env = runner.tier_env(suite, {})
    assert env["TEST_CASES"] == "A-*,B-01" and env["AREAS"] == "x"
    assert "TEST_CASES" not in runner.tier_env({"_tier": {}}, {"TEST_CASES": "stale"})


def test_apply_tier_filter_drops_carried_over_and_unselected():
    suite = {"id": "s", "_tier": {"cases": ["A-*"]}}
    rec = {"cases": [{"id": "A-01", "status": "PASS"}, {"id": "A-02", "status": "FAIL", "carried_over": True},
                     {"id": "B-01", "status": "FAIL"}], "error": None, "status": "FAIL", "counts": {}}
    plan = {"tier": "smoke", "rows": {"s": {"catalog": ["A-01", "A-02", "B-01"], "excluded": ["B-01"]}}}
    out = runner.apply_tier_filter(suite, rec, plan)
    assert [cs["id"] for cs in out["cases"]] == ["A-01"]
    assert out["status"] == "PASS" and out["counts"]["PASS"] == 1
    assert out["tier"]["excluded"] == ["B-01"] and out["tier"]["dropped_rows"] == 2


def test_whole_suite_record_is_untouched():
    rec = {"cases": [{"id": "X", "status": "PASS"}], "status": "PASS"}
    assert runner.apply_tier_filter({"id": "s"}, dict(rec), {"tier": "core", "rows": {}}) == rec


def test_parse_results_json_keeps_carried_over(tmp_path):
    f = tmp_path / "results.json"
    f.write_text(json.dumps({"cases": [{"id": "A", "status": "PASS"}, {"id": "B", "status": "PASS", "carried_over": True}]}))
    cases, _, _ = runner.parse_results_json(f)
    assert "carried_over" not in cases[0] and cases[1]["carried_over"] is True


def _summary(tmp_path, name, finished, suites):
    d = tmp_path / name
    d.mkdir()
    (d / "summary.json").write_text(json.dumps({"run_id": name, "finished": finished, "suites": suites}))
    return d


def test_load_timings_newest_finished_run_wins(tmp_path):
    old = _summary(tmp_path, "old", "2026-01-01", [{"id": "a", "seconds": 10, "status": "PASS", "cases": [{"id": "A-1"}]},
                                                    {"id": "b", "seconds": 5, "status": "PASS", "cases": []}])
    new = _summary(tmp_path, "new", "2026-02-01", [{"id": "a", "seconds": 20, "status": "FAIL", "cases": [{"id": "A-1"}],
                                                    "tier": {"patterns": ["A-*"]}},
                                                   {"id": "b", "seconds": 9, "status": "ERROR", "cases": []}])
    t = runner.load_timings([old, new])
    assert t["a"]["seconds"] == 20 and t["a"]["filtered"] and t["a"]["run"] == "new"
    assert t["b"]["seconds"] == 5  # an ERROR (e.g. timeout) is no timing


def test_case_catalog_unions_timing_and_results_file(tmp_path, monkeypatch):
    f = tmp_path / "results.json"
    f.write_text(json.dumps({"cases": [{"id": "A-2"}, {"id": "A-3", "carried_over": True}]}))
    monkeypatch.setattr(runner, "result_paths", lambda suite, report_dir=None: [f])
    ids = runner.case_catalog({"id": "s", "kind": "command"}, {"cases": ["A-1", "A-2"]})
    assert ids == ["A-1", "A-2", "A-3"]


def test_unpin_last_only_with_relaxed_limits(monkeypatch):
    suites = [{"id": "rp", "layer": "system", "run_last": True}, {"id": "ui", "layer": "ui"}]
    assert [s["id"] for s in runner.unpin_last(suites)] == ["rp", "ui"]
    monkeypatch.setitem(runner._RELAXED, "m", 100)
    assert [s["id"] for s in runner.unpin_last(suites)] == ["rp", "ui"]
    assert all(not s.get("run_last") for s in runner.unpin_last(suites))


def test_render_tier_md_lists_excluded_and_left_out():
    s = {"jobs": 4, "tier": {"name": "smoke", "file": "system_tests/TIERS.json", "estimate_s": 600, "left_out": ["contacts"]},
         "suites": [{"id": "ui", "counts": {"PASS": 1, "FAIL": 0, "BLOCKED": 0},
                     "tier": {"patterns": ["U-01"], "known_cases": 3, "excluded": ["U-02", "U-03"]}},
                    {"id": "unit", "counts": {"PASS": 5, "FAIL": 0, "BLOCKED": 0}}]}
    md = runner.render_tier_md(s)
    assert "## Tier smoke" in md and "| ui | 1 | 3 | 2 | U-01 |" in md and "| unit | 5 | all | 0 | whole suite |" in md
    assert "U-02, U-03" in md and "contacts" in md
    assert runner.render_tier_md({"suites": []}) == ""


def test_run_accepts_tiers_layers_and_new_options():
    ap_args = []

    def fake(args):
        ap_args.append(args)
        return 0
    orig = runner.cmd_run
    runner.cmd_run = fake
    try:
        for target in ("smoke", "core", "full", "all", "unit", "ui"):
            assert runner.main(["run", target, "--jobs", "3", "--skip", "unit", "--dry-run"]) == 0
    finally:
        runner.cmd_run = orig
    assert ap_args[0].layer == "smoke" and ap_args[0].jobs == 3 and ap_args[0].skip == ["unit"]


def test_skip_suites_globs():
    suites = [{"id": "unit"}, {"id": "functional"}, {"id": "ui_features.a"}]
    assert [s["id"] for s in runner.skip_suites(suites, ["unit,ui_features.*"])] == ["functional"]


def test_estimate_steps_pool_and_serial():
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    rows = {k: {"est": 10, "safety": {"locks": []}} for k in "abc"}
    steps = [{"parallel": True, "suites": [a, b]}, {"parallel": False, "suites": [c]}]
    assert runner.estimate_steps(steps, rows, 2) == 20
    assert runner.estimate_steps(steps, rows, 1) == 30


def test_parallel_warning_only_without_multiplier(monkeypatch):
    live = [{"id": "a", "layer": "ui"}, {"id": "b", "layer": "ui"}]
    args = argparse.Namespace(jobs=4)
    assert runner.parallel_warnings(live, args)
    monkeypatch.setitem(runner._RELAXED, "m", 100)
    assert runner.parallel_warnings(live, args) == []
