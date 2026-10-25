"""scripts/ci/check_coverage.py: report parsing, per-package aggregation and the ratchet comparison."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_coverage", ROOT / "scripts" / "ci" / "check_coverage.py")
cc = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage"] = cc  # dataclasses look the module up
_spec.loader.exec_module(cc)

PACKAGES = ["app.routers", "app.services", "app.core", "app.utils", "app.domain", "app.repositories"]


def _json_report(tmp_path, files):
    data = {"files": {name: {"summary": {"num_statements": s, "covered_lines": c, "num_branches": b,
                                         "covered_branches": cb}} for name, (s, c, b, cb) in files.items()}}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(data))
    return path


def _floors(tmp_path, layers, lowered=None):
    path = tmp_path / "floors.json"
    path.write_text(json.dumps({"tolerance_pp": 0.1, "packages": PACKAGES, "layers": layers,
                                "lowered": lowered or []}))
    return path


REPORT = {"app/main.py": (10, 5, 2, 1), "app/core/a.py": (10, 10, 4, 2), "app/routers/r.py": (20, 5, 10, 0),
          "tests/x.py": (100, 0, 0, 0)}


# ── helpers ──

def test_floor2_rounds_down_and_tolerates_float_noise():
    assert cc.floor2(42.199) == 42.19
    assert cc.floor2(57.0) == 57.0
    assert cc.floor2(0.29) == 0.29  # 0.29 * 100 == 28.999999999999996


def test_app_path_and_package():
    assert cc.app_path("app/core/x.py") == "app/core/x.py"
    assert cc.app_path("/work/repo/app/core/x.py") == "app/core/x.py"
    assert cc.app_path("tests/test_x.py") is None
    assert cc.package_of("app/core/x.py") == "app.core"
    assert cc.package_of("app/routers/sub/y.py") == "app.routers"
    assert cc.package_of("app/main.py") is None


def test_percent_of_nothing_is_full():
    assert cc.Counts().percents() == {"line": 100.0, "branch": 100.0}


# ── measure ──

def test_measure_totals_and_packages(tmp_path):
    cur = cc.measure(cc.load_report(str(_json_report(tmp_path, REPORT))), PACKAGES)
    assert cur["app"] == {"line": 50.0, "branch": 18.75}          # 20/40 lines, 3/16 branches; tests/ ignored
    assert cur["app.core"] == {"line": 100.0, "branch": 50.0}
    assert cur["app.routers"] == {"line": 25.0, "branch": 0.0}
    assert "app.services" not in cur                               # absent package: not measured


def test_xml_report_matches_json(tmp_path):
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        '<?xml version="1.0" ?><coverage><sources><source>app</source></sources><packages><package><classes>'
        '<class filename="core/a.py"><lines><line number="1" hits="1"/>'
        '<line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/>'
        '<line number="3" hits="0"/></lines></class>'
        '<class filename="main.py"><lines><line number="1" hits="0" branch="true" condition-coverage="0% (0/2)"/>'
        '</lines></class></classes></package></packages></coverage>')
    cur = cc.measure(cc.load_report(str(xml)), PACKAGES)
    assert cur["app.core"]["line"] == pytest.approx(200 / 3)
    assert cur["app.core"]["branch"] == 50.0
    assert cur["app"] == {"line": 50.0, "branch": 25.0}


# ── compare ──

def _kinds(findings):
    return sorted((f.kind, f.key, f.metric) for f in findings)


def test_compare_within_tolerance_passes():
    cur = {"app": {"line": 49.91, "branch": 20.0}}
    assert [f for f in cc.compare(cur, {"app": {"line": 50.0, "branch": 20.0}}, 0.1) if f.failing] == []


def test_compare_drop_beyond_tolerance_fails():
    cur = {"app": {"line": 49.89, "branch": 20.0}}
    out = cc.compare(cur, {"app": {"line": 50.0, "branch": 20.0}}, 0.1)
    assert _kinds(out) == [("DROP", "app", "line")]
    assert out[0].failing


def test_compare_missing_new_and_above():
    cur = {"app": {"line": 60.0, "branch": 20.0}, "app.domain": {"line": 90.0, "branch": 80.0}}
    floors = {"app": {"line": 50.0, "branch": 20.0}, "app.utils": {"line": 70.0, "branch": 60.0}}
    out = cc.compare(cur, floors, 0.1)
    assert _kinds(out) == [("ABOVE", "app", "line"), ("MISSING", "app.utils", ""), ("NEW", "app.domain", "")]
    assert [f.key for f in out if f.failing] == ["app.utils"]


# ── ratchet ──

def test_raised_never_lowers_and_adds_new_packages():
    floors = {"app": {"line": 50.0, "branch": 20.0}}
    cur = {"app": {"line": 49.95, "branch": 25.678}, "app.core": {"line": 80.0, "branch": 70.0}}
    assert cc.raised(cur, floors, 0.1) == {"app": {"line": 50.0, "branch": 25.67},
                                           "app.core": {"line": 80.0, "branch": 70.0}}


def test_update_refuses_to_lower_without_reason(tmp_path, capsys):
    floors = _floors(tmp_path, {"unit": {"app": {"line": 90.0, "branch": 90.0}}})
    report = _json_report(tmp_path, REPORT)
    rc = cc.main(["--layer", "unit", "--report", str(report), "--floors", str(floors), "--update"])
    assert rc == 1
    assert "refusing to update" in capsys.readouterr().out
    assert json.loads(floors.read_text())["layers"]["unit"]["app"]["line"] == 90.0


def test_update_with_allow_lower_records_reason(tmp_path):
    floors = _floors(tmp_path, {"unit": {"app": {"line": 90.0, "branch": 10.0}, "app.utils": {"line": 1, "branch": 1}}})
    report = _json_report(tmp_path, REPORT)
    rc = cc.main(["--layer", "unit", "--report", str(report), "--floors", str(floors), "--update",
                  "--allow-lower", "moved logic to functional tests"])
    assert rc == 0
    data = json.loads(floors.read_text())
    assert data["layers"]["unit"]["app"] == {"line": 50.0, "branch": 18.75}
    assert "app.utils" not in data["layers"]["unit"]
    log = {(e["key"], e["metric"]): e for e in data["lowered"]}
    assert log[("app", "line")]["from"] == 90.0 and log[("app", "line")]["to"] == 50.0
    assert log[("app.utils", "all")]["to"] is None
    assert {e["reason"] for e in data["lowered"]} == {"moved logic to functional tests"}


def test_update_raises_and_keeps_other_layers(tmp_path):
    floors = _floors(tmp_path, {"functional": {"app": {"line": 70.0, "branch": 60.0}}})
    report = _json_report(tmp_path, REPORT)
    assert cc.main(["--layer", "unit", "--report", str(report), "--floors", str(floors), "--update"]) == 0
    data = json.loads(floors.read_text())
    assert data["layers"]["functional"] == {"app": {"line": 70.0, "branch": 60.0}}
    assert set(data["layers"]["unit"]) == {"app", "app.core", "app.routers"}
    assert data["lowered"] == []


def test_check_fails_without_floors_for_layer(tmp_path):
    floors = _floors(tmp_path, {})
    report = _json_report(tmp_path, REPORT)
    assert cc.main(["--layer", "unit", "--report", str(report), "--floors", str(floors)]) == 1


def test_allow_lower_needs_update_and_reason(tmp_path):
    report = _json_report(tmp_path, REPORT)
    with pytest.raises(SystemExit):
        cc.main(["--layer", "unit", "--report", str(report), "--allow-lower", "x"])
    with pytest.raises(SystemExit):
        cc.main(["--layer", "unit", "--report", str(report), "--update", "--allow-lower", " "])
