#!/usr/bin/env python3
"""Regenerate the L0 import-contract baselines in .importlinter (ADR 0009; docs/CI_GATES.md).

Every forbidden contract whose ``ignore_imports`` starts with the marker line
``# --- BASELINE 2026-10 (shrink only; see REFACTOR_PLAN.md) ---`` owns the lines after that marker:
one ``importer -> imported`` entry per direct import from a source module to a forbidden module, as
grimp (the graph lint-imports uses) sees them. External packages appear under their top-level name
(``sqlalchemy``, ``fastapi``).

lint-imports itself is the gate: it fails on a new import and on a stale entry. This script only keeps
the lists exact.

Usage:
  python scripts/ci/gen_import_baseline.py               report sizes, new and stale entries (exit 1 if any)
  python scripts/ci/gen_import_baseline.py --update      rewrite the blocks; refuses to add entries
  python scripts/ci/gen_import_baseline.py --update --allow-new
                                                        also add entries (reviewer approval needed)
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG = os.path.join(ROOT, ".importlinter")
MARKER = "# --- BASELINE 2026-10 (shrink only; see REFACTOR_PLAN.md) ---"
SECTION_RE = re.compile(r"^\[importlinter:contract:([^\]]+)\]\s*$")


def _within(module: str, parent: str) -> bool:
    return module == parent or module.startswith(parent + ".")


def _modules(value: str) -> list[str]:
    lines = (line.strip() for line in value.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def actual_entries(graph, sources: list[str], forbidden: list[str]) -> list[str]:
    found = set()
    for module in graph.modules:
        if not any(_within(module, src) for src in sources):
            continue
        for imported in graph.find_modules_directly_imported_by(module):
            if any(_within(imported, f) for f in forbidden):
                found.add(f"{module} -> {imported}")
    return sorted(found)


def baseline_blocks(lines: list[str]) -> dict[str, tuple[int, int]]:
    """contract id -> (index of the marker line, index after the block's last line)."""
    blocks: dict[str, tuple[int, int]] = {}
    section = None
    for i, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match:
            section = match.group(1)
        elif section and line.strip() == MARKER:
            end = i + 1
            while end < len(lines) and lines[end].strip() and lines[end][:1].isspace():
                end += 1
            blocks[section] = (i, end)
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="rewrite the baseline blocks")
    parser.add_argument("--allow-new", action="store_true", help="with --update: also add new entries")
    args = parser.parse_args()

    sys.path.insert(0, ROOT)
    import grimp  # dev-only dependency (installed with import-linter)

    with open(CONFIG, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    config = configparser.ConfigParser()
    config.read_string(text)
    blocks = baseline_blocks(lines)
    if not blocks:
        print(f"no '{MARKER}' blocks in .importlinter")
        return 1

    graph = grimp.build_graph("app", include_external_packages=True)
    replacements: dict[str, list[str]] = {}
    failed = False
    for contract, (start, end) in blocks.items():
        section = config[f"importlinter:contract:{contract}"]
        if section.get("allow_indirect_imports", "false").lower() != "true":
            print(f"{contract}: only contracts with allow_indirect_imports = True can be baselined here")
            return 1
        current = [line.strip() for line in lines[start + 1:end] if not line.strip().startswith("#")]
        actual = actual_entries(graph, _modules(section["source_modules"]), _modules(section["forbidden_modules"]))
        new = sorted(set(actual) - set(current))
        stale = sorted(set(current) - set(actual))
        print(f"{contract}: {len(actual)} imports ({len(new)} new, {len(stale)} stale)")
        for entry in new:
            print(f"  + {entry}")
        for entry in stale:
            print(f"  - {entry}")
        if new and not args.allow_new:
            failed = True
        replacements[contract] = actual if args.allow_new else [e for e in actual if e not in new]
        failed = failed or (bool(stale) and not args.update)

    if args.update:
        for contract, (start, end) in sorted(blocks.items(), key=lambda item: -item[1][0]):
            lines[start + 1:end] = [f"    {entry}" for entry in replacements[contract]]
        with open(CONFIG, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("updated .importlinter")
        if failed:
            print("new imports were NOT added: remove them (or rerun with --allow-new and get a reviewer's approval)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
