#!/usr/bin/env python3
"""C-2 type-suppression ratchet (NTS Build Standard Addendum; docs/CI_GATES.md).

Counts `# type: ignore` comments in app/ and fails when the count exceeds the number recorded in
ci/type_ignore_baseline.txt. When the count drops, the gate asks for the baseline to be lowered
(and fails until it is), so the number can only go down. mypy itself runs with warn_unused_ignores.

Usage: python scripts/ci/check_type_ignores.py [--update]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASELINE = os.path.join(ROOT, "ci", "type_ignore_baseline.txt")
PATTERN = re.compile(r"#\s*type:\s*ignore")


def count() -> tuple[int, list[str]]:
    hits = []
    for root, dirs, files in os.walk(os.path.join(ROOT, "app")):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if PATTERN.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{n}")
    return len(hits), hits


def read_baseline() -> int:
    with open(BASELINE, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                return int(line)
    raise SystemExit(f"{BASELINE}: no number found")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="record the current count (only if it is lower)")
    args = ap.parse_args()
    n, hits = count()
    if args.update:
        import datetime
        if os.path.exists(BASELINE) and n > read_baseline():
            print(f"refusing to raise the baseline from {read_baseline()} to {n}")
            return 1
        with open(BASELINE, "w", encoding="utf-8") as fh:
            fh.write(f"# C-2: maximum number of '# type: ignore' comments in app/ (ratchet: may only go down).\n"
                     f"# Updated {datetime.date.today().isoformat()} by scripts/ci/check_type_ignores.py --update\n{n}\n")
    limit = read_baseline()
    print(f"C-2 type: ignore in app/: {n} (baseline {limit})")
    if n > limit:
        print("FAIL: new '# type: ignore' comments; fix the type error instead:")
        print("\n".join(f"  {h}" for h in hits))
        return 1
    if n < limit:
        print(f"FAIL: count dropped to {n}; lower ci/type_ignore_baseline.txt to {n} "
              "(python scripts/ci/check_type_ignores.py --update) so the ratchet holds")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
