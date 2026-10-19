#!/usr/bin/env python3
"""Blocking dependency audit (pip-audit) with a documented, dated ignore list (docs/CI_GATES.md).

Fails when pip-audit reports a vulnerability that is not listed in ci/pip_audit_ignore.txt, when a
listed entry is past its review date or malformed, or when a listed entry no longer matches a finding
(the list may only shrink).

Usage: python scripts/ci/run_pip_audit.py [-r requirements.txt]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
IGNORE = os.path.join(ROOT, "ci", "pip_audit_ignore.txt")


def load_ignores() -> tuple[dict[str, dict], list[str]]:
    entries, errors = {}, []
    with open(IGNORE, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|", 4)]
            if len(parts) != 5 or not all(parts):
                errors.append(f"{IGNORE}:{n}: expected 'id | package | added | review by | reason'")
                continue
            vid, pkg, added, review, reason = parts
            try:
                dt.date.fromisoformat(added)
                review_date = dt.date.fromisoformat(review)
            except ValueError:
                errors.append(f"{IGNORE}:{n}: dates must be YYYY-MM-DD")
                continue
            if review_date < dt.date.today():
                errors.append(f"{IGNORE}:{n}: {vid} ({pkg}) review date {review} has passed: re-assess it")
            entries[vid] = {"package": pkg, "reason": reason}
    return entries, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--requirement", default="requirements.txt")
    args = ap.parse_args()
    ignores, errors = load_ignores()
    proc = subprocess.run([sys.executable, "-m", "pip_audit", "-r", args.requirement, "-f", "json",
                           "--progress-spinner", "off"], cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(proc.stdout[-3000:], proc.stderr[-3000:], sep="\n")
        print("pip-audit did not produce a report")
        return 2
    seen, new = set(), []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []):
            ids = {v["id"], *v.get("aliases", [])}
            hit = ids & set(ignores)
            if hit:
                seen |= hit
                print(f"ACCEPTED {dep['name']} {dep['version']} {v['id']}: {ignores[sorted(hit)[0]]['reason'][:120]}")
            else:
                new.append(f"VULN     {dep['name']} {dep['version']} {v['id']} (fix: {', '.join(v.get('fix_versions') or ['none'])})")
    stale = [f"STALE    {vid} ({e['package']}) no longer reported: delete it from ci/pip_audit_ignore.txt"
             for vid, e in ignores.items() if vid not in seen]
    for line in new + stale + errors:
        print(line)
    print(f"pip-audit: {len(new)} new, {len(seen)} accepted, {len(stale)} stale, {len(errors)} list error(s)")
    return 1 if new or stale or errors else 0


if __name__ == "__main__":
    sys.exit(main())
