from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_core_drills.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("ha_core_drills_script", SCRIPT)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
ha_core_drills = importlib.util.module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = ha_core_drills
_SCRIPT_SPEC.loader.exec_module(ha_core_drills)


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


def test_etcd_restart_prefers_current_leader_metrics_url(monkeypatch, capsys) -> None:
    seen: list[str] = []

    monkeypatch.setattr(ha_core_drills, "_run_shell", lambda _command: None)
    monkeypatch.setattr(
        ha_core_drills,
        "read_etcd_leader",
        lambda *_args, **_kwargs: SimpleNamespace(
            controller_id="core-b",
            controller_epoch=15,
            advertise_addr="http://127.0.0.1:29108",
        ),
    )
    monkeypatch.setattr(
        ha_core_drills,
        "_metrics_reachable",
        lambda url: seen.append(url) or url == "http://127.0.0.1:29108/metrics",
    )

    result = ha_core_drills.etcd_restart(
        argparse.Namespace(
            command="echo restart etcd",
            dry_run=False,
            etcd_endpoints="http://127.0.0.1:2379",
            etcd_prefix="k1s/test",
            metrics_url="http://127.0.0.1:9108/metrics",
            timeout_seconds=1.0,
        )
    )

    assert result == 0
    assert seen == ["http://127.0.0.1:29108/metrics"]
    assert "metrics_url=http://127.0.0.1:29108/metrics" in capsys.readouterr().out


def test_transport_recovery_can_discover_metrics_from_current_leader(monkeypatch) -> None:
    seen: list[str] = []

    monkeypatch.setattr(ha_core_drills, "_run_shell", lambda _command: None)
    monkeypatch.setattr(
        ha_core_drills,
        "read_etcd_leader",
        lambda *_args, **_kwargs: SimpleNamespace(
            controller_id="core-b",
            controller_epoch=15,
            advertise_addr="http://127.0.0.1:39108",
        ),
    )

    def _fake_fetch_metrics(url: str) -> str:
        seen.append(url)
        if url != "http://127.0.0.1:39108/metrics":
            raise RuntimeError(f"unexpected metrics url: {url}")
        return "\n".join(
            [
                'ae_gateway_result_replay_backlog{site="sea"} 0',
                'ae_route_bundle_ack_age_seconds{site="sea"} 0',
                'ae_site_stale{site="sea"} 0',
            ]
        )

    monkeypatch.setattr(ha_core_drills, "_fetch_metrics", _fake_fetch_metrics)

    result = ha_core_drills.transport_recovery(
        argparse.Namespace(
            command="echo restart gateway",
            dry_run=False,
            metrics_url="",
            etcd_endpoints="http://127.0.0.1:2379",
            etcd_prefix="k1s/test",
            site="sea",
            timeout_seconds=1.0,
            backlog_threshold=0.0,
            ack_age_threshold=15.0,
        )
    )

    assert result == 0
    assert seen == ["http://127.0.0.1:39108/metrics"]


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
