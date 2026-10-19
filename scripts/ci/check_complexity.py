#!/usr/bin/env python3
"""C-1 complexity gate (NTS Build Standard Addendum; docs/CI_GATES.md).

Rules enforced on every Python function/method in the repository (radon cyclomatic complexity):
  * no function may exceed MAX_CC (20) unless it is listed in ci/complexity_baseline.json;
  * a baselined function may not get MORE complex than its recorded score (ratchet);
  * the average complexity over all functions must stay below MAX_AVERAGE (5). The code base started
    above it (2026-10-18: 5.24-5.29), so until it gets there the average may not rise above the average
    recorded in the baseline ("average_ceiling", + AVERAGE_TOLERANCE for rounding noise); `--update`
    only ever lowers that ceiling.
Baseline entries whose function has been simplified to <= MAX_CC, renamed or removed are reported
as stale: remove them (the baseline may only shrink). `--update` rewrites the baseline from the current
code, keeping only functions over the limit; use it only to shrink the list or after a reviewed refactor.

Excluded: virtualenvs, node_modules, alembic/versions_legacy (frozen historical migrations), caches.
Usage: python scripts/ci/check_complexity.py [--update [--allow-new]] [--report]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

from radon.complexity import cc_visit
from radon.visitors import Function

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASELINE = os.path.join(ROOT, "ci", "complexity_baseline.json")
MAX_CC = 20
MAX_AVERAGE = 5.0
AVERAGE_TOLERANCE = 0.02
SKIP_DIRS = {".git", "venv", "venv_prod", ".venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "reports"}
SKIP_PATHS = {os.path.join("alembic", "versions_legacy")}


def python_files() -> list[str]:
    out = []
    for root, dirs, files in os.walk(ROOT):
        rel_root = os.path.relpath(root, ROOT)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("venv")
                         and os.path.normpath(os.path.join(rel_root, d)) not in SKIP_PATHS)
        for f in sorted(files):
            if f.endswith(".py"):
                out.append(os.path.normpath(os.path.join(rel_root, f)))
    return out


def _walk(blocks):
    # cc_visit() returns top-level functions, classes and methods (methods flattened, with their class name);
    # nested functions only appear in their parent's `closures`.
    for b in blocks:
        if isinstance(b, Function):
            yield b.fullname, b.complexity
            yield from (( f"{b.fullname}.{n}", c) for n, c in _walk(b.closures))


def measure() -> dict[str, int]:
    scores: dict[str, int] = {}
    for rel in python_files():
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        try:
            blocks = cc_visit(src)
        except SyntaxError as exc:
            print(f"ERROR  {rel}: cannot parse ({exc})")
            sys.exit(2)
        for name, cc in _walk(blocks):
            key = f"{rel.replace(os.sep, '/')}::{name}"
            n, k = 2, key
            while k in scores:  # same qualified name defined twice in one file
                k = f"{key}#{n}"
                n += 1
            scores[k] = cc
    return scores


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the baseline from the current code")
    ap.add_argument("--allow-new", action="store_true",
                    help="with --update: also accept new/worse functions (only for a reviewed, documented exception)")
    ap.add_argument("--report", action="store_true", help="list every function over the limit")
    args = ap.parse_args()

    scores = measure()
    average = sum(scores.values()) / max(len(scores), 1)
    over = {k: v for k, v in sorted(scores.items()) if v > MAX_CC}

    previous: dict = {}
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as fh:
            previous = json.load(fh)
    if args.update:
        ceiling = round(average, 2)
        if previous.get("average_ceiling") is not None:
            ceiling = min(ceiling, float(previous["average_ceiling"]))
        old = previous.get("functions") or {}
        if old:  # never let --update raise a recorded score or add a function silently
            grew = [k for k, v in over.items() if k not in old or v > old[k]]
            if grew and "--allow-new" not in sys.argv:
                print("refusing to update: new/worse functions would enter the baseline:", *grew, sep="\n  ")
                return 1
        data = {"_comment": "C-1 ratchet baseline: functions over cyclomatic complexity "
                            f"{MAX_CC}. Entries may only be removed or lowered (docs/CI_GATES.md).",
                "generated": _dt.date.today().isoformat(), "max_cc": MAX_CC,
                "max_average": MAX_AVERAGE, "average_ceiling": None if ceiling < MAX_AVERAGE else ceiling,
                "functions": over}
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"baseline written: {len(over)} functions over {MAX_CC} ({BASELINE})")

    with open(BASELINE, encoding="utf-8") as fh:
        loaded = json.load(fh)
    baseline: dict[str, int] = loaded["functions"]
    ceiling = loaded.get("average_ceiling")

    failures, stale = [], []
    for key, cc in over.items():
        if key not in baseline:
            failures.append(f"NEW      {key}  cc={cc} > {MAX_CC} (split it up; the baseline is closed to new entries)")
        elif cc > baseline[key]:
            failures.append(f"WORSE    {key}  cc={cc} > baseline {baseline[key]} (the ratchet only goes down)")
    for key, cc in baseline.items():
        if key not in over:
            stale.append(f"STALE    {key}  (baseline {cc}; now {scores.get(key, 'gone')}) - remove it from the baseline")
        elif over[key] < cc:
            stale.append(f"LOWER    {key}  {cc} -> {over[key]} - lower its baseline score")
    if average >= MAX_AVERAGE:
        if ceiling is None:
            failures.append(f"AVERAGE  {average:.2f} >= {MAX_AVERAGE}")
        elif average > float(ceiling) + AVERAGE_TOLERANCE:
            failures.append(f"AVERAGE  {average:.2f} > baseline ceiling {ceiling} (target < {MAX_AVERAGE}; may only go down)")

    if args.report:
        for key, cc in sorted(over.items(), key=lambda kv: -kv[1]):
            print(f"  {cc:4d}  {key}")
    for line in stale:
        print(line)
    for line in failures:
        print(line)
    # A stale entry fails too: otherwise the slot could be reused by a later regression (ratchet).
    print(f"C-1 complexity: {len(scores)} functions, average {average:.2f} (target < {MAX_AVERAGE}, ceiling {ceiling}), "
          f"{len(over)} over {MAX_CC} ({len(baseline)} baselined), {len(failures)} failure(s), {len(stale)} stale")
    return 1 if failures or stale else 0


if __name__ == "__main__":
    sys.exit(main())
