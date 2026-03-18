from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "etcd_snapshot.py"


def test_etcd_snapshot_save_prints_container_command(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.db"
    env = {
        **os.environ,
        "AE_CONTAINER_CLI": "podman",
        "ETCDCTL_BIN": "missing-etcdctl",
        "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
    }

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--runner", "container", "--print-command", "save", "--output", str(output)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    text = proc.stdout.strip()
    assert text.startswith("podman run --rm ")
    assert "quay.io/coreos/etcd:v3.5.13" in text
    assert "etcdctl --endpoints=http://127.0.0.1:2379 snapshot save" in text
    assert str(output) in text


def test_etcd_snapshot_restore_prints_container_command_with_restore_flags(tmp_path: Path) -> None:
    input_path = tmp_path / "snapshot.db"
    data_dir = tmp_path / "restore"
    env = {
        **os.environ,
        "AE_CONTAINER_CLI": "podman",
        "ETCDCTL_BIN": "missing-etcdctl",
    }

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runner",
            "container",
            "--print-command",
            "restore",
            "--input",
            str(input_path),
            "--data-dir",
            str(data_dir),
            "--name",
            "etcd1",
            "--initial-cluster",
            "etcd1=http://10.0.0.2:2380",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    text = proc.stdout.strip()
    assert text.startswith("podman run --rm ")
    assert "snapshot restore" in text
    assert f"--data-dir={data_dir}" in text
    assert "--name=etcd1" in text
    assert "--initial-cluster=etcd1=http://10.0.0.2:2380" in text
