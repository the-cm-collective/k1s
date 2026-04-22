from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT_RUNTIME_ATTRIBUTION = ROOT / "scripts" / "bench" / "audit_runtime_attribution.py"


def _write_summary(path: Path, *, label: str, timestamp: str, runtime_kb: int, app_bytes: int, host_bytes: int, containerd_kb: int, shim_kb: int, conmon_kb: int, host_top: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "label": label,
            "mode": "k1s",
            "timestamp": timestamp,
        },
        "containers": {
            "app_mem_bytes": app_bytes,
        },
        "overhead": {
            "pss_kb_runtime": runtime_kb,
            "host_system_cgroups_bytes": host_bytes,
            "runtime_process_groups": {
                "containerd": containerd_kb,
                "containerd_shim": shim_kb,
                "conmon": conmon_kb,
                "podman": 0,
                "passt": 0,
                "slirp4netns": 0,
                "dockerd": 0,
                "other_runtime": 0,
            },
            "host_system_cgroups_top": host_top,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_audit_runtime_attribution_reports_group_means_and_host_top(tmp_path: Path) -> None:
    exp_a = tmp_path / "exp-a"
    exp_b = tmp_path / "exp-b"
    _write_summary(
        exp_a / "snapshots" / "cri-focus-run1-pods-5" / "20260421-120000" / "summary.json",
        label="cri-focus-run1-pods-5",
        timestamp="20260421-120000",
        runtime_kb=163840,
        app_bytes=95 * 1024 * 1024,
        host_bytes=1410 * 1024 * 1024,
        containerd_kb=81920,
        shim_kb=40960,
        conmon_kb=1024,
        host_top=[
            {
                "path": "/system.slice/systemd-journald.service",
                "slice_kind": "system.slice",
                "bytes": 1000 * 1024 * 1024,
            },
            {
                "path": "/system.slice/containerd.service",
                "slice_kind": "system.slice",
                "bytes": 80 * 1024 * 1024,
            },
        ],
    )
    _write_summary(
        exp_b / "snapshots" / "cri-focus-run2-pods-5" / "20260421-121000" / "summary.json",
        label="cri-focus-run2-pods-5",
        timestamp="20260421-121000",
        runtime_kb=165888,
        app_bytes=96 * 1024 * 1024,
        host_bytes=1412 * 1024 * 1024,
        containerd_kb=86016,
        shim_kb=43008,
        conmon_kb=2048,
        host_top=[
            {
                "path": "/system.slice/systemd-journald.service",
                "slice_kind": "system.slice",
                "bytes": 1002 * 1024 * 1024,
            },
            {
                "path": "/system.slice/containerd.service",
                "slice_kind": "system.slice",
                "bytes": 84 * 1024 * 1024,
            },
        ],
    )

    result = subprocess.run(
        [
            sys.executable,
            str(AUDIT_RUNTIME_ATTRIBUTION),
            str(exp_a),
            str(exp_b),
            "--family",
            "cri-focus-run",
            "--stage",
            "pods-5",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    row = payload[0]
    assert row["family"] == "cri-focus-run"
    assert row["stage"] == "pods-5"
    assert row["rows"] == 2
    assert round(row["runtime_mib"], 1) == 161.0
    assert round(row["app_mib"], 1) == 95.5
    assert round(row["host_system_mib"], 1) == 1411.0
    assert round(row["runtime_groups_mib"]["containerd"], 1) == 82.0
    assert round(row["runtime_groups_mib"]["containerd_shim"], 1) == 41.0
    assert round(row["runtime_groups_mib"]["conmon"], 1) == 1.5
    assert round(row["runtime_groups_mib"]["passt"], 1) == 0.0
    assert round(row["runtime_groups_mib"]["slirp4netns"], 1) == 0.0
    assert row["top_host_services"] == [
        {
            "path": "/system.slice/systemd-journald.service",
            "slice_kind": "system.slice",
            "bytes": 1049624576,
            "mib": 1001.0,
        },
        {
            "path": "/system.slice/containerd.service",
            "slice_kind": "system.slice",
            "bytes": 85983232,
            "mib": 82.0,
        },
    ]
