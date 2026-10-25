#!/usr/bin/env python3
"""Coverage gate with a ratcheting floor (docs/CI_GATES.md "Coverage").

Reads a coverage.py report (JSON from `--cov-report=json`, or Cobertura XML from `--cov-report=xml`) for one
test layer (unit or functional), computes line % and branch % for the whole of `app` and for each top-level
package listed in ci/coverage_floor.json ("packages"), and compares them with that layer's floors:

  * a value more than `tolerance_pp` (0.1 percentage points) below its floor fails;
  * a floor whose package is no longer in the report fails (rename/removal: lower it explicitly, see below);
  * a package in the report without a floor, or a value well above its floor, is only reported.

`--update` raises the layer's floors to the current values (rounded down to 0.01) and adds floors for new
packages. It never lowers a floor: if a value is below its floor by more than the tolerance it refuses,
unless `--allow-lower "REASON"` is given, which lowers those floors (or drops a vanished package) and records
date, layer, value, old/new floor and the reason under "lowered" in the floors file for review.

Usage: python scripts/ci/check_coverage.py --layer unit|functional --report reports/.../coverage.json
                                             [--floors ci/coverage_floor.json] [--update [--allow-lower REASON]]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import posixpath
import sys
# Parses only the coverage report this CI run just produced (trusted input).
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FLOORS = os.path.join(ROOT, "ci", "coverage_floor.json")
DEFAULT_PACKAGES = ["app.routers", "app.services", "app.core", "app.utils", "app.domain", "app.repositories"]
DEFAULT_TOLERANCE = 0.1
METRICS = ("line", "branch")
TOTAL = "app"


@dataclass
class Counts:
    lines: int = 0
    covered_lines: int = 0
    branches: int = 0
    covered_branches: int = 0

    def add(self, other: Counts) -> None:
        self.lines += other.lines
        self.covered_lines += other.covered_lines
        self.branches += other.branches
        self.covered_branches += other.covered_branches

    def percents(self) -> dict[str, float]:
        return {"line": _pct(self.covered_lines, self.lines), "branch": _pct(self.covered_branches, self.branches)}


@dataclass
class Finding:
    kind: str      # DROP, MISSING, NEW, ABOVE
    key: str       # "app" or a package name
    metric: str    # line / branch ("" for MISSING / NEW)
    current: float | None
    floor: float | None

    @property
    def failing(self) -> bool:
        return self.kind in ("DROP", "MISSING")

    def __str__(self) -> str:
        if self.kind == "MISSING":
            return f"MISSING  {self.key}: has a floor but is not in the report (renamed/removed? see --allow-lower)"
        if self.kind == "NEW":
            return f"NEW      {self.key}: no floor yet (record it with --update)"
        verb = "below floor" if self.kind == "DROP" else "above floor (raise it with --update)"
        return f"{self.kind:<8} {self.key} {self.metric}: {self.current:.2f}% {verb} {self.floor:.2f}%"


def _pct(covered: int, total: int) -> float:
    return 100.0 * covered / total if total else 100.0


def floor2(value: float) -> float:
    """Round down to 0.01 so a floor never exceeds the value it was taken from."""
    return math.floor(round(value * 100, 6)) / 100


def app_path(path: str) -> str | None:
    """Normalise a report file name to `app/...` (None for files outside app)."""
    path = path.replace("\\", "/")
    if path.startswith("app/"):
        return path
    i = path.find("/app/")
    return path[i + 1:] if i >= 0 else None


def package_of(path: str) -> str | None:
    """`app/routers/x.py` -> `app.routers`; modules directly in app/ belong to no package."""
    parts = path.split("/")
    return ".".join(parts[:2]) if len(parts) > 2 else None


def load_json_report(path: str) -> dict[str, Counts]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = {}
    for name, info in data["files"].items():
        s = info["summary"]
        out[name] = Counts(s["num_statements"], s["covered_lines"], s.get("num_branches", 0), s.get("covered_branches", 0))
    return out


def _xml_line(line: ET.Element) -> Counts:
    c = Counts(1, 1 if int(line.get("hits", "0")) > 0 else 0)
    cond = line.get("condition-coverage")  # e.g. "50% (1/2)"
    if line.get("branch") == "true" and cond and "(" in cond:
        covered, total = cond.split("(", 1)[1].rstrip(")").split("/")
        c.branches, c.covered_branches = int(total), int(covered)
    return c


def load_xml_report(path: str) -> dict[str, Counts]:
    root = ET.parse(path).getroot()  # nosec B314
    sources = [s.text or "" for s in root.iter("source")]
    prefix = sources[0] if len(sources) == 1 else ""
    out: dict[str, Counts] = {}
    for cls in root.iter("class"):
        name = posixpath.join(prefix.replace("\\", "/"), cls.get("filename", ""))
        counts = out.setdefault(name, Counts())
        for line in cls.iter("line"):
            counts.add(_xml_line(line))
    return out


def load_report(path: str) -> dict[str, Counts]:
    return load_xml_report(path) if path.endswith(".xml") else load_json_report(path)


def measure(files: dict[str, Counts], packages: list[str]) -> dict[str, dict[str, float]]:
    """{"app": {"line": .., "branch": ..}, "app.core": {...}, ...}; only packages with statements in the report."""
    sums: dict[str, Counts] = {TOTAL: Counts()}
    for name, counts in files.items():
        path = app_path(name)
        if path is None:
            continue
        sums[TOTAL].add(counts)
        pkg = package_of(path)
        if pkg in packages:
            sums.setdefault(pkg, Counts()).add(counts)
    return {k: v.percents() for k, v in sums.items() if k == TOTAL or v.lines}


def compare(current: dict, floors: dict, tolerance: float) -> list[Finding]:
    findings = []
    for key, metrics in floors.items():
        if key not in current:
            findings.append(Finding("MISSING", key, "", None, None))
            continue
        for metric, floor in metrics.items():
            value = current[key][metric]
            gap = round(floor - value, 6)
            if gap > tolerance:
                findings.append(Finding("DROP", key, metric, value, floor))
            elif floor2(value) - floor >= 1.0:
                findings.append(Finding("ABOVE", key, metric, value, floor))
    findings += [Finding("NEW", key, "", None, None) for key in current if key not in floors]
    return findings


def raised(current: dict, floors: dict, tolerance: float) -> dict:
    """New floors: max(old, current) per value, new packages added. Drops within tolerance keep the old floor;
    larger drops and missing packages are left as they are (the caller decides about lowering)."""
    out = {key: dict(metrics) for key, metrics in floors.items()}
    for key, metrics in current.items():
        old = out.setdefault(key, {})
        for metric in METRICS:
            old[metric] = max(old.get(metric, 0.0), floor2(metrics[metric]))
    return out


def lowered(current: dict, floors: dict, failing: list[Finding], reason: str, layer: str) -> tuple[dict, list]:
    """Apply an explicit, reasoned lowering for the failing findings; returns (floors, log entries)."""
    out = {key: dict(metrics) for key, metrics in floors.items()}
    today = _dt.date.today().isoformat()
    log = []
    for f in failing:
        entry = {"date": today, "layer": layer, "key": f.key, "metric": f.metric or "all", "from": f.floor,
                 "to": None, "reason": reason}
        if f.kind == "MISSING":
            entry["from"] = out.pop(f.key)
        else:
            entry["to"] = out[f.key][f.metric] = floor2(current[f.key][f.metric])
        log.append(entry)
    return out, log


def read_floors(path: str) -> dict:
    if not os.path.exists(path):
        return {"packages": DEFAULT_PACKAGES, "tolerance_pp": DEFAULT_TOLERANCE, "layers": {}, "lowered": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_floors(path: str, data: dict) -> None:
    data["_comment"] = ("Coverage floors per test layer (line/branch %, branch coverage on). Ratchet: raised by "
                        "scripts/ci/check_coverage.py --update, lowered only with --allow-lower REASON "
                        "(logged under 'lowered'). docs/CI_GATES.md")
    ordered = {k: data[k] for k in ("_comment", "tolerance_pp", "packages", "layers", "lowered") if k in data}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=2)
        fh.write("\n")


def table(layer: str, current: dict, floors: dict) -> list[str]:
    rows = [f"| {layer} | line % | floor | branch % | floor |", "|---|---|---|---|---|"]
    for key in sorted(current, key=lambda k: (k != TOTAL, k)):
        cur, fl = current[key], floors.get(key, {})
        cells = [f"{cur[m]:.2f} | {fl[m]:.2f}" if m in fl else f"{cur[m]:.2f} | -" for m in METRICS]
        rows.append(f"| {key} | " + " | ".join(cells) + " |")
    return rows


def update(data: dict, path: str, layer: str, current: dict, allow_lower: str | None) -> int:
    layer_floors = data["layers"].get(layer, {})
    tol = float(data.get("tolerance_pp", DEFAULT_TOLERANCE))
    failing = [f for f in compare(current, layer_floors, tol) if f.failing]
    if failing and not allow_lower:
        print("refusing to update: these would need a lower floor (pass --allow-lower \"REASON\"):",
              *[f"  {f}" for f in failing], sep="\n")
        return 1
    new = raised(current, layer_floors, tol)
    if failing:
        new, log = lowered(current, new, failing, allow_lower or "", layer)
        data.setdefault("lowered", []).extend(log)
    data["layers"][layer] = new
    write_floors(path, data)
    print(f"floors for layer '{layer}' written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--layer", required=True, help="test layer the report belongs to (unit, functional)")
    ap.add_argument("--report", required=True, help="coverage.json or coverage.xml from pytest-cov")
    ap.add_argument("--floors", default=FLOORS, help="floors file (default ci/coverage_floor.json)")
    ap.add_argument("--update", action="store_true", help="raise this layer's floors to the current values")
    ap.add_argument("--allow-lower", metavar="REASON", help="with --update: lower failing floors, logging REASON")
    args = ap.parse_args(argv)
    if args.allow_lower is not None and (not args.update or not args.allow_lower.strip()):
        ap.error("--allow-lower needs --update and a non-empty reason")

    data = read_floors(args.floors)
    current = measure(load_report(args.report), data.get("packages", DEFAULT_PACKAGES))
    if args.update:
        rc = update(data, args.floors, args.layer, current, args.allow_lower)
        if rc:
            return rc
    return check(data, args.layer, current)


def check(data: dict, layer: str, current: dict) -> int:
    floors = data.get("layers", {}).get(layer)
    if floors is None:
        print(f"FAIL: no floors for layer '{layer}' (record them with --update)")
        return 1
    findings = compare(current, floors, float(data.get("tolerance_pp", DEFAULT_TOLERANCE)))
    rows = table(layer, current, floors)
    print("\n".join(rows))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Coverage ({layer})\n\n" + "\n".join(rows) + "\n\n")
    for f in findings:
        print(f)
    failures = sum(f.failing for f in findings)
    print(f"Coverage ({layer}): {failures} failure(s); tolerance {data.get('tolerance_pp', DEFAULT_TOLERANCE)} pp")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
