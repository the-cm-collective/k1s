from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "smoke_helper.py"

_HELPER_SPEC = spec_from_file_location("smoke_helper_script", HELPER_SCRIPT)
assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
smoke_helper = module_from_spec(_HELPER_SPEC)
sys.modules[_HELPER_SPEC.name] = smoke_helper
_HELPER_SPEC.loader.exec_module(smoke_helper)


def test_smoke_helper_parse_args_rejects_conflicting_passthrough() -> None:
    with pytest.raises(smoke_helper.SmokeHelperError):
        smoke_helper.parse_args(["--down"])


def test_smoke_helper_output_root_follows_passthrough(tmp_path: Path) -> None:
    output_root = smoke_helper.output_root_for(["--output-root", str(tmp_path)])
    assert output_root == tmp_path.resolve()


def test_smoke_helper_lab_python_prefers_repo_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(smoke_helper, "ROOT", tmp_path)

    assert smoke_helper.lab_python() == str(venv_python)


@pytest.mark.parametrize(
    ("passthrough", "teardown", "expected"),
    [
        ([], "on-success", True),
        (["--skip-up"], "never", False),
        (["--skip-up"], "on-success", True),
        (["--plan-only"], "on-success", False),
        (["--plan-only"], "always", True),
    ],
)
def test_smoke_helper_requires_sudo(passthrough: list[str], teardown: str, expected: bool) -> None:
    assert smoke_helper.requires_sudo(passthrough, teardown) is expected


@pytest.mark.parametrize(
    ("policy", "interrupted", "status", "smoke_rc", "expected"),
    [
        ("never", False, "passed", 0, False),
        ("always", False, "failed", 1, True),
        ("always", True, "passed", 0, True),
        ("on-success", False, "passed", 0, True),
        ("on-success", False, "failed", 1, False),
        ("on-success", True, "passed", 0, False),
    ],
)
def test_smoke_helper_should_teardown(
    policy: str, interrupted: bool, status: str, smoke_rc: int, expected: bool
) -> None:
    assert (
        smoke_helper.should_teardown(
            policy,
            interrupted=interrupted,
            summary_status=status,
            smoke_rc=smoke_rc,
        )
        is expected
    )


def test_smoke_helper_run_teardown_skips_when_inventory_missing(tmp_path: Path) -> None:
    result = smoke_helper.run_teardown(
        variant=tmp_path / "variant.yaml",
        run_id="missing-inventory",
        policy="on-success",
        purge=True,
        destroy_network=True,
        interrupted=False,
        summary_status="passed",
        smoke_rc=0,
    )
    assert result.action == "skipped (inventory missing)"
    assert result.returncode == 0


def test_smoke_helper_final_summary_reports_failed_ha_check(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / "demo-run"
    reporter = smoke_helper.Reporter("phase")
    snapshot = smoke_helper.Snapshot(
        summary={
            "status": "failed",
            "run_started_at": "2026-03-19T15:43:20+00:00",
            "run_ended_at": "2026-03-19T15:51:18+00:00",
            "global_phases": [
                {
                    "phase": "provision",
                    "status": "passed",
                    "duration_s": 10.0,
                    "detail": "ok",
                }
            ],
            "lanes": [{"name": "ha_control_plane", "status": "failed"}],
        },
        ha_summary={
            "status": "failed",
            "checks": [
                {
                    "name": "ha_core_cluster_verify",
                    "status": "failed",
                    "optional": False,
                    "detail": "Traceback (most recent call last):",
                    "duration_s": 0.4,
                    "payload": {
                        "stderr": "ssl.SSLCertVerificationError: certificate verify failed"
                    },
                }
            ],
        },
    )

    reporter.print_final_summary(
        run_id="demo-run",
        variant=ROOT / "lab" / "variants" / "ha-control-plane-core.yaml",
        run_dir=run_dir,
        snapshot=snapshot,
        teardown_result=smoke_helper.TeardownResult(action="skipped (policy=on-success)"),
        capture=smoke_helper.StreamCapture(),
        smoke_rc=1,
    )

    out = capsys.readouterr().out
    assert "status=failed" in out
    assert "ha_core_cluster_verify: failed" in out
    assert "first_failure=HA check ha_core_cluster_verify" in out
    assert "certificate verify failed" in out
    assert str(run_dir) in out


def test_smoke_helper_first_failure_prefers_global_phase_stderr_tail() -> None:
    known_hosts_warning = (
        "Warning: Permanently added '192.168.155.10' (ED25519) to the list of known hosts."
    )
    snapshot = smoke_helper.Snapshot(
        summary={
            "status": "failed",
            "global_phases": [
                {
                    "phase": "bootstrap",
                    "status": "failed",
                    "detail": known_hosts_warning,
                    "stderr_tail": [
                        known_hosts_warning,
                        "python: command not found",
                    ],
                }
            ],
            "lanes": [],
        }
    )

    failure = smoke_helper.first_failure(snapshot)
    assert failure == ("global phase bootstrap", "python: command not found")
