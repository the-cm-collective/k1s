from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from scripts.qualification import kp1_q1_conductor as conductor
from scripts.qualification import kp1_q1_live as live


class _Transport:
    def __init__(self, result: live.Completed) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def execute(self, argv, *, timeout=live.SSH_COMMAND_SECONDS):  # type: ignore[no-untyped-def]
        self.calls.append((tuple(argv), timeout))
        return self.result


def _authority() -> conductor.RunAuthority:
    return conductor.RunAuthority(
        run_id="q1-0123456789abcdef0123456789abcdef",
        plan_digest="sha256:" + "1" * 64,
        bundle_id="sha256:" + "2" * 64,
        bundle_sha256="sha256:" + "3" * 64,
        source_commit="4" * 40,
        source_tree="5" * 40,
        transaction_id="q1tx-0123456789abcdef0123456789abcdef",
    )


def test_file_replay_ledger_is_exclusive(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    ledger = live.FileReplayLedger(root)
    claim = "sha256:" + "a" * 64

    assert ledger.claim(claim) is True
    assert ledger.claim(claim) is False
    receipt = next(root.iterdir())
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert receipt.read_text(encoding="ascii") == claim + "\n"


def test_ssh_target_is_loopback_strict_and_key_pinned(tmp_path: Path) -> None:
    key = tmp_path / "id"
    known = tmp_path / "known_hosts"
    target = live.SshTarget(40222, key, known)

    argv = target.common_argv()

    assert argv[0] == "/usr/bin/ssh"
    assert "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={known}" in argv
    assert argv[-1] == "q1@127.0.0.1"
    assert "-A" not in argv


def test_ssh_stage_adapter_preserves_closed_stage_argv_and_authority() -> None:
    spec = next(item for item in conductor.STAGE_SPECS if item.name == "offline-install")
    transport = _Transport(live.Completed(0, b'{"status":"passed"}\n', b""))
    adapter = live.SshStageAdapter(transport)  # type: ignore[arg-type]
    authority = _authority().as_dict()
    request = conductor.StageRequest(
        authority=authority,
        stage=spec,
        deadline_monotonic=time.monotonic() + 60,
    )

    observation = adapter.execute(request)

    assert transport.calls[0][0] == spec.argv
    assert observation.identity == authority
    assert observation.exit_code == 0
    assert observation.evidence[0].kind == "guest-stage-result"


def test_ssh_stage_adapter_reports_nonzero_without_reclassifying_output() -> None:
    spec = next(item for item in conductor.STAGE_SPECS if item.name == "offline-install")
    transport = _Transport(live.Completed(7, b"", b"failed"))
    adapter = live.SshStageAdapter(transport)  # type: ignore[arg-type]
    observation = adapter.execute(
        conductor.StageRequest(
            authority=_authority().as_dict(),
            stage=spec,
            deadline_monotonic=time.monotonic() + 60,
        )
    )
    assert observation.exit_code == 7
    assert observation.stderr == b"failed"


def test_qmp_json_client_negotiates_and_returns_exact_response(tmp_path: Path) -> None:
    path = tmp_path / "qmp.sock"
    ready = threading.Event()
    errors: list[BaseException] = []

    def server() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(b'{"QMP":{"version":{}}}\n')
                    assert json.loads(connection.recv(4096)) == {"execute": "qmp_capabilities"}
                    connection.sendall(b'{"return":{}}\n')
                    assert json.loads(connection.recv(4096)) == {"execute": "query-status"}
                    connection.sendall(b'{"return":{"status":"running"}}\n')
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert ready.wait(5)

    response = live.QmpJsonClient(path).command("query-status", timeout=2, maximum=4096)
    thread.join(5)

    assert not errors
    assert response == {"return": {"status": "running"}}


def test_qmp_json_client_ignores_bounded_events_and_preserves_stream_buffer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "qmp-events.sock"
    ready = threading.Event()
    errors: list[BaseException] = []

    def server() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(b'{"QMP":{"version":{}}}\n{"event":"RESET","data":{}}\n')
                    assert json.loads(connection.recv(4096)) == {"execute": "qmp_capabilities"}
                    connection.sendall(b'{"event":"RESUME","data":{}}\n{"return":{}}\n')
                    assert json.loads(connection.recv(4096)) == {"execute": "query-status"}
                    connection.sendall(
                        b'{"event":"STOP","data":{}}\n{"return":{"status":"running"}}\n'
                    )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    assert ready.wait(5)

    response = live.QmpJsonClient(path).command("query-status", timeout=2, maximum=4096)
    thread.join(5)

    assert not errors
    assert response == {"return": {"status": "running"}}


def test_qmp_json_client_rejects_unknown_command(tmp_path: Path) -> None:
    client = live.QmpJsonClient(tmp_path / "missing")
    with pytest.raises(live.LivePolicyError):
        client.command("human-monitor-command", timeout=1, maximum=4096)


def test_inventory_is_metadata_safe_and_stable() -> None:
    first = live._inventory_entries()
    second = live._inventory_entries()

    assert first == second
    assert first
    assert all(item.kind in {"network-interface", "runtime-object"} for item in first)
    assert all(item.identity.startswith("sha256:") for item in first)


def test_remove_verified_run_root_refuses_nonprivate_root(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir(mode=0o755)
    with pytest.raises(live.LivePolicyError):
        live._remove_verified_run_root(root)


def test_execute_live_recovers_exact_context_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_root = tmp_path / "run"
    capsule_parent = tmp_path / "capsules"
    process = object()
    qmp_client = object()
    recovered: list[dict[str, object]] = []

    def fail_started(**kwargs):  # type: ignore[no-untyped-def]
        kwargs["failure_context"].update(
            {
                "capsule_parent": capsule_parent,
                "process": process,
                "qmp_client": qmp_client,
                "retained_evidence_root": tmp_path / "evidence",
                "run_id": "q1-0123456789abcdef0123456789abcdef",
                "run_root": run_root,
            }
        )
        raise RuntimeError("stage failed")

    def recover(**kwargs):  # type: ignore[no-untyped-def]
        recovered.append(dict(kwargs))

    monkeypatch.setattr(live, "_execute_live_unguarded", fail_started)
    monkeypatch.setattr(live, "_recover_failed_capsule", recover)

    with pytest.raises(RuntimeError, match="stage failed"):
        live.execute_live(
            plan={},
            expected_plan_digest="sha256:" + "1" * 64,
            host_preflight_path=tmp_path / "preflight.json",
            expected_host_preflight_digest="sha256:" + "2" * 64,
            gate_a_path=tmp_path / "gate-a.json",
            expected_gate_a_digest="sha256:" + "3" * 64,
            input_bundle_path=tmp_path / "bundle",
            expected_input_bundle_digest="sha256:" + "4" * 64,
            expected_bundle_id="sha256:" + "5" * 64,
            ssh_private_key=tmp_path / "id",
            authority=_authority(),
            retained_evidence_root=tmp_path / "evidence",
            replay_ledger_root=tmp_path / "replay",
        )

    assert len(recovered) == 1
    assert recovered[0]["run_root"] == run_root
    assert recovered[0]["capsule_parent"] == capsule_parent
    assert recovered[0]["process"] is process
    assert recovered[0]["qmp_client"] is qmp_client
