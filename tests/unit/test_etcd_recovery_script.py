from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "etcd_recovery.py"


def test_member_add_print_command_uses_learner_flag() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runner",
            "container",
            "--print-command",
            "--endpoints",
            "http://10.0.0.11:2379,http://10.0.0.12:2379",
            "member-add",
            "--name",
            "etcd-c",
            "--peer-url",
            "http://10.0.0.13:2380",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout.strip()
    assert "member add etcd-c" in text
    assert "--peer-urls=http://10.0.0.13:2380" in text
    assert "--learner" in text


def test_quorum_restore_plan_prints_three_member_layout(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.db"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "quorum-restore-plan",
            "--input",
            str(snapshot),
            "--cluster-token",
            "k1s-ha",
            "--member",
            "etcd-a=http://10.0.0.11:2380",
            "--member",
            "etcd-b=http://10.0.0.12:2380",
            "--member",
            "etcd-c=http://10.0.0.13:2380",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout.strip()
    assert "Quorum restore plan from snapshot:" in text
    assert "[etcd-a]" in text
    assert "[etcd-c]" in text
    assert "--initial-cluster-state=new" in text
