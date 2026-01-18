#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

try:
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - packaging may not be installed in all envs
    InvalidVersion = None
    Version = None


FALLBACK_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.dev\d+)?$")


def read_pyproject_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not version:
        raise ValueError("project.version is missing in pyproject.toml")
    if not isinstance(version, str):
        raise ValueError("project.version must be a string")
    return version


def validate_version(version: str) -> None:
    if Version is not None:
        try:
            Version(version)
        except InvalidVersion as exc:  # pragma: no cover - depends on packaging
            raise ValueError(f"invalid PEP 440 version: {version}") from exc
        return
    if not FALLBACK_VERSION_RE.match(version):
        raise ValueError("invalid version format; expected PEP 440 like 0.1.0a1 or 0.1.0a2.dev0")


def changelog_has_release(changelog: Path, version: str) -> bool:
    for line in changelog.read_text(encoding="utf-8").splitlines():
        if not line.startswith("## "):
            continue
        heading = line[3:].strip()
        if heading == version or heading.startswith(f"{version} -"):
            return True
    return False


def detect_tag() -> str | None:
    env = os.environ
    if env.get("GITHUB_REF_TYPE") == "tag":
        return env.get("GITHUB_REF_NAME") or None
    ref = env.get("GITHUB_REF")
    if ref and ref.startswith("refs/tags/"):
        return ref.split("/", 2)[2]
    for key in ("CI_COMMIT_TAG", "GIT_TAG", "TAG", "BUILD_TAG"):
        value = env.get(key)
        if value:
            return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate project versioning rules.")
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument(
        "--require-changelog",
        action="store_true",
        help="Always require a changelog section for the current version.",
    )
    args = parser.parse_args()

    pyproject_path = Path(args.pyproject)
    changelog_path = Path(args.changelog)

    errors: list[str] = []
    try:
        version = read_pyproject_version(pyproject_path)
        validate_version(version)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        version = None

    if version and (args.require_changelog or ".dev" not in version):
        if not changelog_path.exists():
            errors.append(f"missing changelog: {changelog_path}")
        elif not changelog_has_release(changelog_path, version):
            errors.append(f"CHANGELOG.md missing section for version {version}")

    tag = detect_tag()
    if version and tag and tag != f"v{version}":
        errors.append(f"tag {tag} does not match version v{version}")

    if errors:
        for error in errors:
            print(f"versioning: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
