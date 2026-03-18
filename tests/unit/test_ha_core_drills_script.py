from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_core_drills.py"


def test_leader_failover_dry_run_prints_summary() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "leader-failover",
            "--command",
            "echo stop leader",
            "--dry-run",
            "--require-controller-change",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout.strip()
    assert "DRY RUN leader-failover" in text
    assert "require_controller_change=True" in text


def test_transport_recovery_dry_run_prints_thresholds() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "transport-recovery",
            "--command",
            "echo restart nats",
            "--metrics-url",
            "http://127.0.0.1:9108/metrics",
            "--site",
            "sea",
            "--backlog-threshold",
            "1",
            "--ack-age-threshold",
            "9",
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    text = proc.stdout.strip()
    assert "DRY RUN transport-recovery" in text
    assert "site=sea" in text
    assert "backlog_threshold=1.0" in text
    assert "ack_age_threshold=9.0" in text
