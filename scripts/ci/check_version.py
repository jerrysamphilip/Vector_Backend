#!/usr/bin/env python3
"""Release identity gate (docs/CI_GATES.md): one version number, everywhere in this repository.

Checks that
  * VERSION holds a SemVer number;
  * CHANGELOG.md has a `## [<VERSION>]` section, and it is the newest numbered section;
  * app/core/config.py does not hard-code a version (APP_VERSION comes from VERSION);
  * every image of this application in manifests/*.yaml (name containing vector/outreach/sales360/backend)
    is the `<ECR_IMAGE_URI>` placeholder or tagged `:<VERSION>` / `:v<VERSION>`; digest-pinned third-party
    images (docs/RELEASE.md, scripts/pin_images.py) are not release-numbered and are skipped;
  * every "release X.Y.Z" / "version X.Y.Z" mention in docs/*.md names VERSION.
Usage: python scripts/ci/check_version.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    errors: list[str] = []
    version = read("VERSION").strip()
    if not SEMVER.match(version):
        errors.append(f"VERSION: {version!r} is not a SemVer number")

    sections = re.findall(r"^## \[(\d+\.\d+\.\d+[^\]]*)\]", read("CHANGELOG.md"), re.M)
    if version not in sections:
        errors.append(f"CHANGELOG.md: no '## [{version}]' section")
    elif sections[0] != version:
        errors.append(f"CHANGELOG.md: newest section is {sections[0]}, VERSION is {version}")

    config = read("app/core/config.py")
    m = re.search(r"APP_VERSION\s*:\s*str\s*=\s*['\"]([^'\"]+)['\"]", config)
    if m:
        errors.append(f"app/core/config.py: APP_VERSION is hard-coded ({m.group(1)}); read it from VERSION")

    for path in sorted(glob.glob(os.path.join(ROOT, "manifests", "*.y*ml"))):
        for n, line in enumerate(read(os.path.relpath(path, ROOT)).splitlines(), 1):
            im = re.match(r"\s*image:\s*(\S+)", line)
            if not im or im.group(1).strip("'\"") == "<ECR_IMAGE_URI>" or "@sha256:" in im.group(1):
                continue
            if not re.search(r"vector|outreach|sales360|backend", im.group(1).split("/")[-1], re.I):
                continue
            tag = im.group(1).rsplit(":", 1)[-1] if ":" in im.group(1).split("/")[-1] else ""
            if tag.lstrip("v") != version:
                errors.append(f"{os.path.relpath(path, ROOT)}:{n}: image {im.group(1)} is not tagged {version}")

    for path in sorted(glob.glob(os.path.join(ROOT, "docs", "*.md"))):
        rel = os.path.relpath(path, ROOT)
        for n, line in enumerate(read(rel).splitlines(), 1):
            for vm in re.finditer(r"\b(?:release|version)\s+v?(\d+\.\d+\.\d+)\b", line, re.I):
                if vm.group(1) != version:
                    errors.append(f"{rel}:{n}: mentions {vm.group(0)!r}, VERSION is {version}")

    for e in errors:
        print("FAIL ", e)
    print(f"release identity: VERSION {version}, {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
