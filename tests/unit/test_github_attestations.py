from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_github_attestations.py"


def _bundle(root: Path) -> Path:
    bundle = root / "bundle"
    dist = bundle / "dist"
    manifests = bundle / "manifests"
    dist.mkdir(parents=True)
    manifests.mkdir()
    for relative in (
        "artifact-manifest.json",
        "SHA256SUMS",
        "dist/k1s_core-1.0.0rc2-py3-none-any.whl",
        "dist/k1s_core-1.0.0rc2.tar.gz",
        "manifests/source-members.jsonl",
        "manifests/wheel-members.jsonl",
        "manifests/sdist-members.jsonl",
    ):
        (bundle / relative).write_text(relative + "\n", encoding="utf-8")
    return bundle


def _fake_gh(root: Path) -> tuple[Path, Path]:
    executable = root / "gh"
    log = root / "gh.jsonl"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with Path(os.environ["FAKE_GH_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
subject = Path(sys.argv[3]).name
if subject == os.environ.get("FAKE_GH_FAIL_SUBJECT"):
    raise SystemExit(17)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _run(
    bundle: Path, fake_gh: Path, log: Path, *, fail_subject: str = ""
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_FAIL_SUBJECT": fail_subject,
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [
            sys.executable,
            str(SCRIPT),
            "--bundle-root",
            str(bundle),
            "--repository",
            "the-cm-collective/k1s",
            "--gh",
            str(fake_gh),
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_verifier_calls_github_cli_once_per_exact_subject(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    fake_gh, log = _fake_gh(tmp_path)

    result = _run(bundle, fake_gh, log)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "repository": "the-cm-collective/k1s",
        "status": "verified",
        "subjectCount": 7,
        "subjects": [
            "dist/k1s_core-1.0.0rc2-py3-none-any.whl",
            "dist/k1s_core-1.0.0rc2.tar.gz",
            "artifact-manifest.json",
            "SHA256SUMS",
            "manifests/source-members.jsonl",
            "manifests/wheel-members.jsonl",
            "manifests/sdist-members.jsonl",
        ],
    }
    calls = _calls(log)
    assert len(calls) == 7
    for call, relative in zip(calls, summary["subjects"], strict=True):
        assert call == [
            "attestation",
            "verify",
            str((bundle / relative).resolve()),
            "--repo",
            "the-cm-collective/k1s",
        ]


@pytest.mark.parametrize("mutation", ["missing", "extra", "symlink"])
def test_verifier_rejects_nonexact_bundle_before_github_calls(
    tmp_path: Path, mutation: str
) -> None:
    bundle = _bundle(tmp_path)
    fake_gh, log = _fake_gh(tmp_path)
    if mutation == "missing":
        (bundle / "manifests" / "source-members.jsonl").unlink()
    elif mutation == "extra":
        (bundle / "manifests" / "extra.jsonl").write_text("extra\n", encoding="utf-8")
    else:
        subject = bundle / "manifests" / "source-members.jsonl"
        subject.unlink()
        subject.symlink_to(bundle / "SHA256SUMS")

    result = _run(bundle, fake_gh, log)

    assert result.returncode == 1
    assert "verify-github-attestations:" in result.stderr
    assert _calls(log) == []


def test_verifier_stops_on_first_failed_subject(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    fake_gh, log = _fake_gh(tmp_path)

    result = _run(bundle, fake_gh, log, fail_subject="artifact-manifest.json")

    assert result.returncode == 1
    assert "artifact-manifest.json: exit 17" in result.stderr
    calls = _calls(log)
    assert [Path(call[2]).name for call in calls] == [
        "k1s_core-1.0.0rc2-py3-none-any.whl",
        "k1s_core-1.0.0rc2.tar.gz",
        "artifact-manifest.json",
    ]
