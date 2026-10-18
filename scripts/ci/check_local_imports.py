#!/usr/bin/env python3
"""C-5 function-local import gate (NTS Build Standard Addendum; docs/CI_GATES.md).

Imports inside a function body hide dependencies from the import contracts (.importlinter still sees
them, but reviewers do not) and usually paper over an import cycle. Every function-local import in
app/ must be listed in ci/local_imports_baseline.txt as `path:function:module`; that file is the
register of local imports documented as intentional or pending a fix (the comment block at its top
says how to read it). The gate fails on any local import that is not listed, and on listed entries
that no longer exist (delete them: the register may only shrink).

Usage: python scripts/ci/check_local_imports.py [--update]
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASELINE = os.path.join(ROOT, "ci", "local_imports_baseline.txt")
SCAN = ["app"]
HEADER = """\
# C-5 register of function-local imports in app/ (checked by scripts/ci/check_local_imports.py).
# Format: path:function:module  (function is the dotted qualified name; module is what is imported).
# Baseline taken {date}. Status of every entry: PENDING unless marked otherwise in a trailing
# "  # intentional: <reason>" comment. Typical reasons a local import is kept on purpose: optional
# dependency (import only when the feature is used), start-up cost, or breaking a known cycle that
# .importlinter lists in its baseline. New local imports are rejected: import at module level, or add
# the entry here in the same reviewed change with an "# intentional:" reason. Entries may only be removed.
"""


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.depth = 0  # function nesting depth
        self.found: list[str] = []

    def _scope(self, node, is_func: bool):
        self.stack.append(node.name)
        self.depth += is_func
        self.generic_visit(node)
        self.depth -= is_func
        self.stack.pop()

    def visit_ClassDef(self, node):
        self._scope(node, False)

    def visit_FunctionDef(self, node):
        self._scope(node, True)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _add(self, module: str):
        if self.depth:
            self.found.append(f"{self.rel}:{'.'.join(self.stack)}:{module}")

    def visit_Import(self, node):
        for alias in node.names:
            self._add(alias.name)

    def visit_ImportFrom(self, node):
        self._add("." * node.level + (node.module or ""))


def scan() -> list[str]:
    found: list[str] = []
    for top in SCAN:
        for root, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
                v = _Visitor(rel)
                v.visit(tree)
                found.extend(v.found)
    return found


def load() -> dict[str, str]:
    entries: dict[str, str] = {}
    with open(BASELINE, encoding="utf-8") as fh:
        for line in fh:
            body, _, comment = line.partition("#")
            body = body.strip()
            if body:
                entries[body] = comment.strip()
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the register from the current code")
    args = ap.parse_args()
    current = sorted(set(scan()))

    if args.update:
        import datetime
        old = load() if os.path.exists(BASELINE) else {}
        with open(BASELINE, "w", encoding="utf-8") as fh:
            fh.write(HEADER.format(date=datetime.date.today().isoformat()))
            for e in current:
                fh.write(f"{e}  # {old[e]}\n" if old.get(e) else f"{e}\n")
        print(f"register written: {len(current)} entries")

    listed = load()
    new = [e for e in current if e not in listed]
    gone = [e for e in listed if e not in set(current)]
    for e in new:
        print(f"NEW    {e}  (move it to module level, or register it with an '# intentional:' reason)")
    for e in gone:
        print(f"STALE  {e}  (no longer in the code: delete the line)")
    print(f"C-5 local imports: {len(current)} found, {len(listed)} registered, {len(new)} new, {len(gone)} stale")
    return 1 if new or gone else 0


if __name__ == "__main__":
    sys.exit(main())
