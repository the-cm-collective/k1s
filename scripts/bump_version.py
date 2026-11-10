#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from scripts.project_version import read_project_version, write_project_version
except ModuleNotFoundError:  # direct execution from scripts/
    from project_version import read_project_version, write_project_version

VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<pre_label>a|b|rc)(?P<pre_num>\d+))?"
    r"(?:\.dev(?P<dev_num>\d+))?$"
)


def bump_next_dev(version: str) -> str:
    match = VERSION_RE.match(version)
    if not match:
        raise ValueError(f"unsupported version format: {version}")
    if match.group("dev_num") is not None:
        raise ValueError(f"version already has .dev: {version}")
    pre_label = match.group("pre_label")
    pre_num = match.group("pre_num")
    if pre_label is None or pre_num is None:
        raise ValueError("final versions are not supported; tag a pre-release (a/b/rc) first")
    next_pre = int(pre_num) + 1
    base = f"{match.group('major')}.{match.group('minor')}.{match.group('patch')}"
    return f"{base}{pre_label}{next_pre}.dev0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump to the next .dev0 version.")
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the next version without writing."
    )
    args = parser.parse_args()

    path = Path(args.pyproject)
    try:
        current = read_project_version(path)
        next_version = bump_next_dev(current)
    except (OSError, ValueError) as exc:
        print(f"bump-version: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(next_version)
        return 0

    try:
        write_project_version(path, next_version)
    except (OSError, ValueError) as exc:
        print(f"bump-version: {exc}", file=sys.stderr)
        return 1

    print(f"{current} -> {next_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
