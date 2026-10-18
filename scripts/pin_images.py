#!/usr/bin/env python3
"""Pin container images by digest (rule P-2: never deploy a mutable tag).

Scans Dockerfile* (FROM lines), docker-compose*/compose* files and manifests/**/*.y(a)ml
(image: lines) and rewrites every third-party reference to ``name:tag@sha256:<digest>``.
The tag stays in the reference so humans can read it and the next refresh knows what to
resolve; with a digest present, Docker and Kubernetes ignore the tag.

    python3 scripts/pin_images.py              # resolve every tag again and rewrite (run before each release)
    python3 scripts/pin_images.py --dry-run    # show what would change
    python3 scripts/pin_images.py --check      # no network: fail if any FROM/image line lacks @sha256
    python3 scripts/pin_images.py --check --strict manifests/deployment.yaml
                                               # also fail on <PLACEHOLDER> images (rendered manifest, CI)

Skipped (allowed by --check):
  * build-stage names (``FROM builder``) and ``scratch``
  * placeholders such as ``<ECR_IMAGE_URI>`` that CI replaces with the pushed image *by digest*
    (not allowed with --strict)
  * lines carrying a ``# pin:ignore <reason>`` comment (e.g. compose names for locally built images);
    in a Dockerfile, which has no trailing comments, put it on the line directly above the FROM

Digests are resolved with ``docker buildx imagetools inspect`` (no pull). If Docker Hub rate-limits,
set PIN_MIRROR=mirror.gcr.io to resolve Docker Hub images through Google's mirror: the digest of a
mirrored manifest list is identical, and the rewritten line keeps the Docker Hub name.
Standard library only, so it runs on a bare CI runner.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FROM_RE = re.compile(
    r"^(?P<pre>\s*FROM\s+(?:--platform=\S+\s+)?)(?P<ref>\S+)(?P<post>(?:\s+AS\s+(?P<stage>\S+))?\s*(?:#.*)?)$",
    re.IGNORECASE,
)
IMAGE_RE = re.compile(r"""^(?P<pre>\s*(?:-\s+)?image:\s*)(?P<q>["']?)(?P<ref>[^\s"'#]+)(?P=q)(?P<post>\s*(?:#.*)?)$""")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
PLACEHOLDER_RE = re.compile(r"^<[^>]+>$|^\$\{?[A-Za-z_]")
IGNORE_MARK = "pin:ignore"


def default_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in ("Dockerfile*", "**/Dockerfile*", "docker-compose*.y*ml", "compose*.y*ml", "manifests/**/*.y*ml"):
        for path in ROOT.glob(pattern):
            if path.is_file() and "node_modules" not in path.parts and ".git" not in path.parts:
                files.add(path)
    return sorted(files)


def iter_refs(path: Path):
    """Yield (line_index, match, kind, ref) for every FROM / image line of a file."""
    stages: set[str] = set()
    is_dockerfile = path.name.startswith("Dockerfile")
    prev = ""
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(keepends=False)):
        m = FROM_RE.match(line) if is_dockerfile else IMAGE_RE.match(line)
        # Dockerfiles have no trailing comments: there the marker goes on the line above.
        marker = prev if prev.lstrip().startswith("#") else ""
        prev = line
        if not m:
            continue
        ref = m.group("ref")
        line = f"{line} {marker}"
        if is_dockerfile:
            is_stage_ref = ref.lower() in stages or ref.lower() == "scratch"
            if m.group("stage"):
                stages.add(m.group("stage").lower())
            if is_stage_ref:
                continue
        yield i, m, ref, line


def classify(ref: str, line: str) -> str:
    if IGNORE_MARK in line:
        return "ignored"
    if PLACEHOLDER_RE.search(ref):
        return "placeholder"
    if DIGEST_RE.search(ref):
        return "pinned"
    return "unpinned"


def split_ref(ref: str) -> tuple[str, str | None]:
    """'nginx:1.2@sha256:..' -> ('nginx', '1.2'). Registry ports ('host:5000/x') are kept."""
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
        return name, tag
    return name, None


def resolution_ref(name: str, tag: str) -> str:
    mirror = os.environ.get("PIN_MIRROR", "").strip().rstrip("/")
    first = name.split("/", 1)[0]
    is_docker_hub = "/" not in name or not ("." in first or ":" in first or first == "localhost")
    if first == "docker.io":
        is_docker_hub, name = True, name.split("/", 1)[1]
    if mirror and is_docker_hub:
        repo = name if "/" in name else f"library/{name}"
        return f"{mirror}/{repo}:{tag}"
    return f"{name}:{tag}"


def resolve_digest(ref: str) -> str:
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    digest = json.loads(out)["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"unexpected digest {digest!r} for {ref}")
    return digest


def check(files: list[Path], strict: bool) -> int:
    problems = []
    for path in files:
        for i, _m, ref, line in iter_refs(path):
            kind = classify(ref, line)
            if kind == "unpinned" or (strict and kind == "placeholder"):
                problems.append(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{i + 1}: "
                                f"{kind} image {ref!r}")
    if problems:
        print("Images must be pinned by digest (P-2). Run `python3 scripts/pin_images.py` and commit:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"pin_images: OK, every image in {len(files)} file(s) is pinned by digest")
    return 0


def pin(files: list[Path], dry_run: bool) -> int:
    cache: dict[str, str] = {}
    failures = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        changed = False
        for i, m, ref, line in list(iter_refs(path)):
            if classify(ref, line) in {"ignored", "placeholder"}:
                continue
            name, tag = split_ref(ref)
            if not tag:
                print(f"{path}:{i + 1}: {ref!r} has no tag; add one (e.g. :1.2-alpine) so it can be re-resolved",
                      file=sys.stderr)
                failures += 1
                continue
            key = f"{name}:{tag}"
            if key not in cache:
                try:
                    cache[key] = resolve_digest(resolution_ref(name, tag))
                except Exception as exc:  # noqa: BLE001 - report and keep going
                    print(f"{path}:{i + 1}: cannot resolve {key}: {exc}", file=sys.stderr)
                    failures += 1
                    continue
            new_ref = f"{key}@{cache[key]}"
            if new_ref == ref:
                continue
            q = m.groupdict().get("q") or ""
            eol = lines[i][len(lines[i].rstrip("\r\n")):]
            lines[i] = f"{m.group('pre')}{q}{new_ref}{q}{m.group('post')}{eol}"
            changed = True
            print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{i + 1}: {ref} -> {new_ref}")
        if changed and not dry_run:
            path.write_text("".join(lines), encoding="utf-8")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("files", nargs="*", type=Path, help="files to process (default: Dockerfiles, compose, manifests/)")
    parser.add_argument("--check", action="store_true", help="only verify; no network")
    parser.add_argument("--strict", action="store_true", help="with --check: placeholders are failures too")
    parser.add_argument("--dry-run", action="store_true", help="resolve and print, do not rewrite files")
    args = parser.parse_args(argv)
    files = [p.resolve() for p in args.files] or default_files()
    if args.check:
        return check(files, args.strict)
    return pin(files, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
