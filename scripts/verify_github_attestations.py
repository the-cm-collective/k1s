#!/usr/bin/env python3
"""Verify every expected publication-bundle subject with GitHub attestations."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_FILES = {"SHA256SUMS", "artifact-manifest.json"}
ROOT_DIRECTORIES = {"dist", "manifests"}
MANIFEST_FILES = {
    "sdist-members.jsonl",
    "source-members.jsonl",
    "wheel-members.jsonl",
}
WHEEL_NAME = re.compile(r"^k1s_core-[0-9A-Za-z.]+-py3-none-any\.whl$")
SDIST_NAME = re.compile(r"^k1s_core-[0-9A-Za-z.]+\.tar\.gz$")
EXPECTED_SUBJECT_COUNT = 7


class AttestationPolicyError(ValueError):
    """Raised when a publication bundle does not match the attestation policy."""


def _entry_names(directory: Path) -> set[str]:
    try:
        return {entry.name for entry in directory.iterdir()}
    except OSError as exc:
        raise AttestationPolicyError(f"cannot inspect bundle directory: {directory}") from exc


def publication_subjects(bundle_root: Path) -> tuple[Path, ...]:
    """Return the seven exact, regular, non-symlink publication subjects."""
    root = bundle_root.resolve(strict=True)
    if bundle_root.is_symlink() or not root.is_dir():
        raise AttestationPolicyError("bundle root must be a real directory")

    expected_root = ROOT_FILES | ROOT_DIRECTORIES
    if _entry_names(root) != expected_root:
        raise AttestationPolicyError(
            "bundle root entries differ from the exact publication contract"
        )

    dist = root / "dist"
    manifests = root / "manifests"
    if dist.is_symlink() or manifests.is_symlink() or not dist.is_dir() or not manifests.is_dir():
        raise AttestationPolicyError("bundle dist and manifests entries must be real directories")

    dist_names = _entry_names(dist)
    wheels = sorted(name for name in dist_names if WHEEL_NAME.fullmatch(name))
    sdists = sorted(name for name in dist_names if SDIST_NAME.fullmatch(name))
    if len(wheels) != 1 or len(sdists) != 1 or dist_names != {wheels[0], sdists[0]}:
        raise AttestationPolicyError(
            "bundle dist must contain exactly one canonical wheel and sdist"
        )
    if _entry_names(manifests) != MANIFEST_FILES:
        raise AttestationPolicyError("bundle manifests differ from the exact publication contract")

    subjects = (
        dist / wheels[0],
        dist / sdists[0],
        root / "artifact-manifest.json",
        root / "SHA256SUMS",
        manifests / "source-members.jsonl",
        manifests / "wheel-members.jsonl",
        manifests / "sdist-members.jsonl",
    )
    if len(subjects) != EXPECTED_SUBJECT_COUNT:
        raise AttestationPolicyError("internal subject-count policy error")
    for subject in subjects:
        if subject.is_symlink() or not subject.is_file():
            raise AttestationPolicyError(f"attestation subject is not a regular file: {subject}")
    return subjects


def verify_subjects(
    *, bundle_root: Path, repository: str, gh_command: str = "gh"
) -> dict[str, object]:
    """Invoke GitHub CLI exactly once for each expected publication subject."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AttestationPolicyError("repository must use owner/name form")
    executable = shutil.which(gh_command)
    if executable is None:
        raise AttestationPolicyError(f"GitHub CLI is unavailable: {gh_command}")

    root = bundle_root.resolve(strict=True)
    subjects = publication_subjects(bundle_root)
    verified: list[str] = []
    for subject in subjects:
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed argument contract
            [executable, "attestation", "verify", str(subject), "--repo", repository],
            check=False,
        )
        if result.returncode != 0:
            relative = subject.relative_to(root).as_posix()
            raise AttestationPolicyError(
                f"GitHub attestation verification failed for {relative}: exit {result.returncode}"
            )
        verified.append(subject.relative_to(root).as_posix())
    return {
        "repository": repository,
        "status": "verified",
        "subjectCount": len(verified),
        "subjects": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the seven exact GitHub-attested publication-bundle subjects."
    )
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--gh", default="gh", help="GitHub CLI executable (default: gh)")
    args = parser.parse_args()
    try:
        result = verify_subjects(
            bundle_root=args.bundle_root,
            repository=args.repository,
            gh_command=args.gh,
        )
    except (AttestationPolicyError, OSError) as exc:
        print(f"verify-github-attestations: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
