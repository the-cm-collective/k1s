from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECK_IDLE_SNAPSHOT = ROOT / "scripts" / "bench" / "check_idle_snapshot.py"


def _write_snapshot(tmp_path: Path, *, app_bytes: int, inspect_payload: list[dict] | None = None) -> Path:
    snapshot_dir = tmp_path / "snap"
    raw_dir = snapshot_dir / "raw"
    raw_dir.mkdir(parents=True)
    (snapshot_dir / "summary.json").write_text(
        json.dumps({"containers": {"app_mem_bytes": app_bytes}}),
        encoding="utf-8",
    )
    if inspect_payload is not None:
        (raw_dir / "cri_inspect.json").write_text(json.dumps(inspect_payload), encoding="utf-8")
    return snapshot_dir


def test_check_idle_snapshot_accepts_clean_snapshot(tmp_path: Path) -> None:
    snapshot_dir = _write_snapshot(tmp_path, app_bytes=0, inspect_payload=[])

    result = subprocess.run(
        [sys.executable, str(CHECK_IDLE_SNAPSHOT), str(snapshot_dir), "--app-name", "echo"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_check_idle_snapshot_rejects_nonzero_app_memory(tmp_path: Path) -> None:
    snapshot_dir = _write_snapshot(tmp_path, app_bytes=1024)

    result = subprocess.run(
        [sys.executable, str(CHECK_IDLE_SNAPSHOT), str(snapshot_dir), "--app-name", "echo"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "idle app_mem_bytes=1024 exceeds max_app_bytes=0" in result.stdout


def test_check_idle_snapshot_rejects_app_owned_cri_container_labels(tmp_path: Path) -> None:
    snapshot_dir = _write_snapshot(
        tmp_path,
        app_bytes=0,
        inspect_payload=[
            {
                "Id": "abc1234567890000",
                "Name": "main",
                "Labels": {"ae.app": "echo", "ae.replica_id": "echo-rev7-0"},
            }
        ],
    )

    result = subprocess.run(
        [sys.executable, str(CHECK_IDLE_SNAPSHOT), str(snapshot_dir), "--app-name", "echo"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "idle snapshot still captured app-owned containers" in result.stdout
    assert "cri_inspect.json:abc123456789:main" in result.stdout


def test_check_idle_snapshot_rejects_replica_only_match(tmp_path: Path) -> None:
    snapshot_dir = _write_snapshot(
        tmp_path,
        app_bytes=0,
        inspect_payload=[
            {
                "Id": "def1234567890000",
                "Name": "main",
                "Config": {"Labels": {"ae.replica_id": "echo-rev9-1"}},
            }
        ],
    )

    result = subprocess.run(
        [sys.executable, str(CHECK_IDLE_SNAPSHOT), str(snapshot_dir), "--app-name", "echo"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "cri_inspect.json:def123456789:main" in result.stdout
