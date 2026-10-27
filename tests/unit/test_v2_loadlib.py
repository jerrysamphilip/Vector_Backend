"""Chunk V2: pure parts of the load-test tooling (scripts/load/loadlib.py and run_load.py helpers)."""
import importlib.util
import random
import sys
from pathlib import Path

import pytest

LOAD_DIR = Path(__file__).resolve().parents[2] / "scripts" / "load"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"v2_{name}", LOAD_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


loadlib = _load("loadlib")
run_load = _load("run_load")


# ── percentile ──────────────────────────────────────────────────────────────

def test_percentile_empty_is_none():
    assert loadlib.percentile([], 95) is None


def test_percentile_single_value():
    assert loadlib.percentile([42], 50) == 42
    assert loadlib.percentile([42], 99) == 42


def test_percentile_interpolates_like_numpy_linear():
    values = list(range(1, 101))  # 1..100
    assert loadlib.percentile(values, 0) == 1
    assert loadlib.percentile(values, 100) == 100
    assert loadlib.percentile(values, 50) == pytest.approx(50.5)
    assert loadlib.percentile(values, 95) == pytest.approx(95.05)
    assert loadlib.percentile(values, 99) == pytest.approx(99.01)


def test_percentile_ignores_input_order():
    assert loadlib.percentile([30, 10, 20], 50) == 20
    assert loadlib.percentile([10, 20, 30, 40], 50) == pytest.approx(25)


def test_percentile_rejects_out_of_range():
    with pytest.raises(ValueError):
        loadlib.percentile([1, 2], 101)


# ── summaries ───────────────────────────────────────────────────────────────

def _samples():
    S = loadlib.Sample
    return [S("a", 200, 100, True, "http://r1"), S("a", 200, 300, True, "http://r2"),
            S("a", 500, 900, False, "http://r1"), S("b", 403, 50, False, "http://r2"),
            S("b", 200, 70, True, "http://r1")]


def test_summarize_per_endpoint_and_all():
    out = loadlib.summarize(_samples(), duration_s=10)
    assert out["a"]["count"] == 3 and out["a"]["errors"] == 1
    assert out["a"]["error_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert out["a"]["p50"] == 300 and out["a"]["max"] == 900
    assert out["a"]["statuses"] == {"200": 2, "500": 1}
    assert out["ALL"]["count"] == 5 and out["ALL"]["errors"] == 2
    assert out["ALL"]["rps"] == 0.5


def test_summarize_by_replica():
    out = loadlib.summarize(_samples(), duration_s=5, key="base_url")
    assert out["http://r1"]["count"] == 3 and out["http://r2"]["count"] == 2


def test_summarize_zero_duration_has_no_rate():
    assert loadlib.summarize(_samples(), duration_s=0)["ALL"]["rps"] == 0.0


# ── weighted mix ────────────────────────────────────────────────────────────

def test_mix_respects_weights():
    mix = loadlib.WeightedMix({"a": 3, "b": 1, "never": 0})
    rng = random.Random(7)
    picks = [mix.pick(rng) for _ in range(20000)]
    assert "never" not in picks
    assert picks.count("a") / len(picks) == pytest.approx(0.75, abs=0.02)
    assert mix.share("a") == pytest.approx(0.75) and mix.share("never") == 0.0


def test_mix_is_deterministic_per_seed():
    mix = loadlib.WeightedMix(run_load.DEFAULT_MIX)
    one = [mix.pick(random.Random(3)) for _ in range(5)]
    two = [mix.pick(random.Random(3)) for _ in range(5)]
    assert one == two


def test_mix_needs_a_positive_weight():
    with pytest.raises(ValueError):
        loadlib.WeightedMix({"a": 0})
    with pytest.raises(ValueError):
        loadlib.WeightedMix({"a": 1, "b": -1})


def test_parse_mix_overrides_only_named_entries():
    out = loadlib.parse_mix("a=5, c=0", {"a": 1, "b": 2, "c": 3}, known={"a", "b", "c"})
    assert out == {"a": 5.0, "b": 2, "c": 0.0}


@pytest.mark.parametrize("text", ["a", "zzz=1"])
def test_parse_mix_rejects_bad_entries(text):
    with pytest.raises(ValueError):
        loadlib.parse_mix(text, {"a": 1}, known={"a"})


def test_default_mix_names_are_real_scenarios():
    assert set(run_load.DEFAULT_MIX) == set(run_load.SCENARIOS)


# ── thresholds ──────────────────────────────────────────────────────────────

def test_parse_target_and_direction():
    t = loadlib.parse_target("contacts_list.p95=1000")
    assert (t.endpoint, t.metric, t.value, t.op) == ("contacts_list", "p95", 1000.0, "<=")
    assert loadlib.parse_target("ALL.rps=50").op == ">="


@pytest.mark.parametrize("text", ["contacts_list.p95", "contacts_list.p42=1", "x.p95=fast"])
def test_parse_target_rejects_bad_input(text):
    with pytest.raises(ValueError):
        loadlib.parse_target(text)


def test_merge_targets_overrides_same_key():
    defaults = [loadlib.Target("a", "p95", 1000, "req"), loadlib.Target("ALL", "error_rate", 0, "req")]
    merged = loadlib.merge_targets(defaults, [loadlib.Target("a", "p95", 2500, "CLI")])
    assert {(t.endpoint, t.metric): t.value for t in merged} == {("a", "p95"): 2500, ("ALL", "error_rate"): 0}


def test_evaluate_pass_fail_skip():
    summary = {"a": {"count": 10, "p95": 900.0, "error_rate": 0.0, "rps": 4.0},
               "b": {"count": 2, "p95": 5000.0},
               "ALL": {"count": 12, "p95": 1200.0, "error_rate": 0.1, "rps": 4.0}}
    targets = [loadlib.Target("a", "p95", 1000), loadlib.Target("b", "p95", 1000),
               loadlib.Target("missing", "p95", 1000), loadlib.Target("ALL", "error_rate", 0.0),
               loadlib.Target("ALL", "rps", 5)]
    verdicts = loadlib.evaluate(summary, targets, min_samples=5)
    assert [v["status"] for v in verdicts] == ["PASS", "SKIP", "SKIP", "FAIL", "FAIL"]
    assert loadlib.breached(verdicts)


def test_evaluate_boundary_is_inclusive():
    verdicts = loadlib.evaluate({"a": {"count": 9, "p95": 1000.0}}, [loadlib.Target("a", "p95", 1000)])
    assert verdicts[0]["status"] == "PASS" and not loadlib.breached(verdicts)


def test_default_targets_cover_the_requirements():
    keys = {(t.endpoint, t.metric): t.value for t in run_load.DEFAULT_TARGETS}
    assert keys[("contacts_list", "p95")] == 1000       # §6 lists < 1 s at 100k
    assert keys[("reports_dashboard", "p95")] == 3000   # RP-130
    assert keys[("ALL", "error_rate")] == 0.0           # RP-131


def test_markdown_report_has_tables_and_verdict():
    summary = loadlib.summarize(_samples(), 10)
    verdicts = loadlib.evaluate(summary, [loadlib.Target("a", "p95", 10_000)])
    md = loadlib.markdown_report({"concurrency": 5}, summary, loadlib.summarize(_samples(), 10, "base_url"), verdicts)
    assert "| a | 3 | 1 |" in md and "## Per API replica" in md and "**Overall: PASS**" in md


# ── run_load helpers ────────────────────────────────────────────────────────

def test_parse_args_applies_mix_and_targets():
    args = run_load.parse_args(["--base-url", "http://x:1/", "--base-url", "http://y:2", "--mix", "export_small=0",
                                "--target", "contacts_list.p95=2000", "--duration", "30", "--ramp", "5"])
    assert args.base_url == ["http://x:1", "http://y:2"]
    assert args.weights["export_small"] == 0 and args.weights["contacts_list"] == 25
    assert {(t.endpoint, t.metric): t.value for t in args.targets}[("contacts_list", "p95")] == 2000


def test_parse_args_rejects_ramp_longer_than_duration():
    with pytest.raises(SystemExit):
        run_load.parse_args(["--duration", "5", "--ramp", "10"])


def test_vu_addresses_are_distinct():
    assert len({run_load.vu_address(i) for i in range(1000)}) == 1000


def test_items_of_finds_the_row_list():
    assert run_load.items_of({"items": [1], "total": 1}) == [1]
    assert run_load.items_of([{"x": 1}]) == [{"x": 1}]
    assert run_load.items_of({"nothing": 1}) == []
