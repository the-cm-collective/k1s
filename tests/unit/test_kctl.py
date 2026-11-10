"""k1s (kubectl-like) CLI smoke tests."""

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

import ae.kctl.__main__ as kctl_main
from ae.kctl.__main__ import build_parser, main
from ae.kctl.lifecycle import LifecycleResult
from ae.kctl.profile_backend import PortableLifecycleResult
from ae.kctl.profile_engine import EngineCapability
from ae.kctl.profile_host_transaction import (
    HostTransactionResult,
    ProfileHostTransactionError,
)
from ae.kctl.profile_node_adapter import StrictCRINodeInputs
from ae.kctl.profile_state import ProfileLogSnapshot
from ae.kctl.profiles import InstalledProfileFacts


def write_manifest(path: Path, image: str = "alpine:3.20") -> None:
    path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: {image}
  replicas: 1
        """.strip()
    )


def test_get_describe_rollout_and_logs(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "echo.yaml"
    write_manifest(manifest)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "applied default/echo" in out

    # get apps
    assert main(["get", "apps"]) == 0
    out = capsys.readouterr().out
    assert "default/echo" in out and "ready=1" in out

    # get single app
    assert main(["get", "app", "echo"]) == 0
    out = capsys.readouterr().out
    assert "default/echo: desired=1" in out

    # describe
    assert main(["describe", "app/echo"]) == 0
    out = capsys.readouterr().out
    assert "event" in out
    assert "pod" in out or "- echo-rev" in out

    # rollout history
    assert main(["rollout", "history", "echo", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "rev 1" in out

    # logs
    assert main(["logs", "app/echo"]) == 0
    out = capsys.readouterr().out
    assert "echo-rev1-0" in out


def test_rollout_undo(tmp_path, monkeypatch, capsys):
    v1 = tmp_path / "echo.yaml"
    write_manifest(v1, image="alpine:3.20")
    v2 = tmp_path / "echo-v2.yaml"
    write_manifest(v2, image="alpine:3.21")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(v1)]) == 0
    capsys.readouterr()
    assert main(["apply", "-f", str(v2)]) == 0
    capsys.readouterr()

    assert main(["rollout", "undo", "app/echo", "--to-revision", "1"]) == 0
    out = capsys.readouterr().out
    assert "rolled back default/echo" in out


def test_profile_lifecycle_cli_does_not_initialize_workload_runtime(monkeypatch, capsys):
    calls: list[tuple[str, str]] = []

    class FakeManager:
        def perform(self, action: str, profile: str) -> LifecycleResult:
            calls.append((action, profile))
            return LifecycleResult(
                action="start",
                profile="core-edge",
                status="started",
                exit_code=0,
                pid=1234,
                command=("/repo/scripts/dev/run_profile.sh", "k1s-core"),
            )

    monkeypatch.setattr(kctl_main, "profile_lifecycle_manager_factory", FakeManager)
    monkeypatch.setattr(
        kctl_main,
        "_setup",
        lambda: (_ for _ in ()).throw(AssertionError("workload runtime initialized")),
    )

    assert main(["profile", "start", "core-edge", "--output", "json"]) == 0
    assert calls == [("start", "core-edge")]
    assert '"profile": "core-edge"' in capsys.readouterr().out


def test_k1s_help_preserves_workload_commands_and_adds_profile_lifecycle() -> None:
    help_text = build_parser().format_help()
    for command in ("get", "describe", "apply", "rollout", "logs", "events", "delete", "scale"):
        assert command in help_text
    assert "profile" in help_text


def test_profile_inspection_cli_is_non_mutating(monkeypatch, tmp_path, capsys) -> None:
    class StaticProbe:
        def inspect(self) -> InstalledProfileFacts:
            return InstalledProfileFacts(
                python_version=(3, 13),
                platform="linux",
                ae_importable=True,
                distribution_version="1.0.0a2",
                k1s_console_present=True,
            )

    def fail(*args, **kwargs):
        del args, kwargs
        raise AssertionError("non-mutating profile command crossed a side-effect boundary")

    cwd_before = Path.cwd()
    environment_before = dict(os.environ)
    files_before = tuple(tmp_path.iterdir())

    with monkeypatch.context() as guard:
        guard.setattr(kctl_main, "profile_doctor_probe_factory", StaticProbe)
        guard.setattr(kctl_main, "_setup", fail)
        guard.setattr(kctl_main, "profile_lifecycle_manager_factory", fail)
        guard.setattr(kctl_main, "portable_profile_manager_factory", fail)
        guard.setattr(kctl_main, "profile_engine_probe_factory", fail)
        guard.setattr(kctl_main, "runtime_factory", fail)
        guard.setattr(kctl_main, "state_store_from_env", fail)
        guard.setattr(subprocess, "Popen", fail)
        guard.setattr(subprocess, "run", fail)
        guard.setattr(socket, "create_connection", fail)
        guard.setattr(os, "kill", fail)
        guard.setattr(os, "killpg", fail)
        guard.setattr(os, "putenv", fail)
        guard.setattr(os, "unsetenv", fail)
        guard.setattr(Path, "mkdir", fail)
        guard.setattr(Path, "write_text", fail)
        guard.setattr(Path, "write_bytes", fail)
        guard.setattr(Path, "unlink", fail)
        guard.setattr(Path, "replace", fail)

        assert main(["profile", "list", "--output", "json"]) == 0
        catalog = json.loads(capsys.readouterr().out)
        assert catalog["mutating"] is False

        assert main(["profile", "plan", "edge-gateway", "--output", "json"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["components"] == ["gateway"]
        assert "worker-stub" not in plan["components"]

        assert main(["profile", "doctor", "core", "--output", "json"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        assert doctor["mutating"] is False
        assert doctor["overallStatus"] == "warn"

    assert Path.cwd() == cwd_before
    assert dict(os.environ) == environment_before
    assert tuple(tmp_path.iterdir()) == files_before


def test_profile_doctor_probes_only_the_explicit_selected_engine(
    monkeypatch,
    capsys,
) -> None:
    class StaticProbe:
        def inspect(self) -> InstalledProfileFacts:
            return InstalledProfileFacts(
                python_version=(3, 13),
                platform="linux",
                ae_importable=True,
                distribution_version="1.0.0a2",
                k1s_console_present=True,
            )

    calls: list[str] = []

    class EngineProbe:
        def probe(self, backend: str) -> EngineCapability:
            calls.append(backend)
            return EngineCapability(
                backend="podman",
                executable=Path("/usr/bin/podman"),
                executable_device=1,
                executable_inode=2,
                version="5.0.0",
                rootless=True,
            )

    monkeypatch.setattr(kctl_main, "profile_doctor_probe_factory", StaticProbe)
    monkeypatch.setattr(kctl_main, "profile_engine_probe_factory", EngineProbe)

    assert main(["profile", "doctor", "local", "--engine", "podman", "-o", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == ["podman"]
    assert payload["engineCapability"]["backend"] == "podman"
    assert payload["qualificationPosture"] == "source-validated/unqualified"


def test_local_and_core_route_to_portable_manager_without_legacy_fallback(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    class PortableManager:
        def perform(
            self,
            action: str,
            profile: str,
            *,
            backend: str | None = None,
            bootstrap_id: str | None = None,
        ) -> PortableLifecycleResult:
            assert bootstrap_id is None
            calls.append((action, profile, backend))
            return PortableLifecycleResult(
                action="start",
                profile="local",
                status="started",
                backend="podman",
                run_id="r1-00000000000000000000000000000000",
                generation=1,
                phase="running",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", PortableManager)
    monkeypatch.setattr(
        kctl_main,
        "profile_lifecycle_manager_factory",
        lambda: (_ for _ in ()).throw(AssertionError("legacy fallback used")),
    )

    assert main(["profile", "start", "local", "--backend", "podman", "-o", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [("start", "local", "podman")]
    assert payload["qualification_posture"] == "source-validated/unqualified"


def test_profile_inspection_json_never_discloses_environment_values(
    monkeypatch,
    capsys,
) -> None:
    sentinel = "DO-NOT-PRINT-THIS-SECRET"
    monkeypatch.setenv("K1S_PROFILE_TEST_SECRET", sentinel)

    class StaticProbe:
        def inspect(self) -> InstalledProfileFacts:
            return InstalledProfileFacts(
                python_version=(3, 13),
                platform="linux",
                ae_importable=True,
                distribution_version="1.0.0a2",
                k1s_console_present=True,
            )

    monkeypatch.setattr(kctl_main, "profile_doctor_probe_factory", StaticProbe)
    assert main(["profile", "doctor", "edge-dev", "--output", "json"]) == 0
    output = capsys.readouterr().out
    assert sentinel not in output
    assert "K1S_PROFILE_TEST_SECRET" not in output


def test_profile_help_exposes_non_mutating_contracts_and_legacy_actions() -> None:
    parser = build_parser()
    profile_action = next(
        action for action in parser._actions if getattr(action, "dest", None) == "cmd"
    )
    profile_parser = profile_action.choices["profile"]
    help_text = profile_parser.format_help()
    for command in (
        "list",
        "doctor",
        "plan",
        "start",
        "status",
        "stop",
        "restart",
        "run",
        "logs",
        "delete",
        "bootstrap",
        "host",
    ):
        assert command in help_text


def test_strict_cri_host_plan_cli_routes_only_exact_authority(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []
    payload = {
        "kind": "StrictCRIHostPlan",
        "mutating": False,
        "planDigest": "sha256:" + "1" * 64,
        "profileId": "core-a",
        "qualificationPosture": "source-validated/unqualified",
        "role": "core",
        "target": {"targetId": "guest-a"},
        "actions": [],
        "warnings": [],
    }

    def plan(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(kctl_main, "plan_strict_cri_from_files", plan)
    monkeypatch.setattr(
        kctl_main,
        "_setup",
        lambda: (_ for _ in ()).throw(AssertionError("workload runtime initialized")),
    )
    assert (
        main(
            [
                "profile",
                "host",
                "plan",
                "strict-cri",
                "--intent",
                "/private/intent.json",
                "--expect-intent-sha256",
                "sha256:" + "a" * 64,
                "--facts",
                "/private/facts.json",
                "--expect-facts-sha256",
                "sha256:" + "b" * 64,
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "expected_facts_digest": "sha256:" + "b" * 64,
            "expected_intent_digest": "sha256:" + "a" * 64,
            "facts_path": "/private/facts.json",
            "intent_path": "/private/intent.json",
        }
    ]
    rendered = json.loads(capsys.readouterr().out)
    assert rendered == payload
    assert rendered["mutating"] is False
    assert rendered["qualificationPosture"] == "source-validated/unqualified"


def test_strict_cri_host_plan_cli_error_is_generic_and_redacted(monkeypatch, capsys) -> None:
    private = "DO-NOT-PRINT-/private/intent.json"

    def fail(**_kwargs):
        raise kctl_main.ProfileHostPlanError(private)

    monkeypatch.setattr(kctl_main, "plan_strict_cri_from_files", fail)
    assert (
        main(
            [
                "profile",
                "host",
                "plan",
                "strict-cri",
                "--intent",
                private,
                "--expect-intent-sha256",
                "sha256:" + "a" * 64,
                "--facts",
                "/private/facts.json",
                "--expect-facts-sha256",
                "sha256:" + "b" * 64,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "strict CRI host plan failed closed"
    assert private not in captured.err


def test_profile_run_records_signal_intent_and_returns_foreground_exit(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []

    class Manager:
        def run(  # type: ignore[no-untyped-def]
            self, profile, *, backend, bootstrap_id, stop_requested
        ):
            assert bootstrap_id is None
            calls.append((profile, backend))
            assert stop_requested() is None
            return PortableLifecycleResult(
                action="run",
                profile="core",
                status="externally-stopped",
                backend="podman",
                run_id="r1-00000000000000000000000000000001",
                generation=1,
                phase="stopped",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", Manager)
    assert main(["profile", "run", "core", "-o", "json"]) == 0
    assert calls == [("core", None)]
    assert json.loads(capsys.readouterr().out)["action"] == "run"


def test_profile_logs_and_delete_route_only_exact_arguments(monkeypatch, capsys) -> None:
    calls: list[tuple[object, ...]] = []
    run_id = "r1-00000000000000000000000000000001"

    class Manager:
        def logs(self, profile, *, component, run_id, tail):  # type: ignore[no-untyped-def]
            calls.append(("logs", profile, component, run_id, tail))
            return ProfileLogSnapshot(profile, run_id, component, ("safe",), False)

        def delete(self, profile, *, run_id, confirmation):  # type: ignore[no-untyped-def]
            calls.append(("delete", profile, run_id, confirmation))
            return PortableLifecycleResult(
                action="delete",
                profile="core",
                status="deleted",
                backend="podman",
                run_id=run_id,
                generation=1,
                phase="stopped",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", Manager)
    assert (
        main(
            [
                "profile",
                "logs",
                "core",
                "--component",
                "controller",
                "--run-id",
                run_id,
                "--tail",
                "1",
                "-o",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["lines"] == ["safe"]
    assert (
        main(
            [
                "profile",
                "delete",
                "core",
                "--run-id",
                run_id,
                "--confirm-delete",
                f"core/{run_id}",
                "-o",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "deleted"
    assert calls == [
        ("logs", "core", "controller", run_id, 1),
        ("delete", "core", run_id, f"core/{run_id}"),
    ]


def test_profile_run_logs_delete_reject_nonportable_roles_at_parse_time() -> None:
    parser = build_parser()
    for argv in (
        ["profile", "run", "edge-dev"],
        ["profile", "logs", "edge-dev", "--component", "worker"],
        [
            "profile",
            "delete",
            "edge-dev",
            "--run-id",
            "r1-00000000000000000000000000000001",
            "--confirm-delete",
            "edge-dev/r1-00000000000000000000000000000001",
        ],
    ):
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover - fail closed assertion
            raise AssertionError("nonportable lifecycle role unexpectedly parsed")


def test_profile_bootstrap_create_outputs_only_public_projection(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    sentinel = "DO-NOT-PRINT-NODE-TOKEN"
    source = tmp_path / "node-token"
    source.write_text(sentinel, encoding="utf-8")
    source.chmod(0o600)
    calls: list[tuple[str, dict[str, object]]] = []

    class Record:
        def public_projection(self) -> dict[str, object]:
            return {
                "advertiseEndpoint": "https://node-a.example.test:9109",
                "backendId": "podman",
                "bootstrapId": "b1-" + "1" * 32,
                "controllerUrl": "https://controller.example.test:9443",
                "identityVersion": 1,
                "listenHost": "127.0.0.1",
                "listenPort": 9109,
                "nodeId": "node-a",
                "profile": "node",
                "siteId": None,
                "transportUrl": None,
            }

    class Store:
        def create_node(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("node", kwargs))
            return Record()

    monkeypatch.setattr(kctl_main, "profile_bootstrap_store_factory", lambda _profile: Store())

    assert (
        main(
            [
                "profile",
                "bootstrap",
                "create",
                "node",
                "--node-id",
                "node-a",
                "--controller-url",
                "https://controller.example.test:9443",
                "--runtime-backend",
                "podman",
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                "9109",
                "--advertise-endpoint",
                "https://node-a.example.test:9109",
                "--credential-file",
                str(source),
                "-o",
                "json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert calls[0][0] == "node"
    assert calls[0][1]["credential_file"] == source
    assert payload["identityVersion"] == 1
    assert sentinel not in output
    assert str(source) not in output
    assert "credential" not in output.lower()


@pytest.mark.parametrize(
    "failure",
    [
        kctl_main.ProfileStateError("DO-NOT-PRINT-PRIVATE-BOOTSTRAP"),
        OSError("DO-NOT-PRINT-/private/bootstrap-record"),
        kctl_main.ProfilePathError("DO-NOT-PRINT-/private/bootstrap-directory"),
    ],
)
def test_profile_bootstrap_error_never_renders_private_exception(
    monkeypatch,
    capsys,
    failure: Exception,
) -> None:
    class Store:
        def create_edge_gateway(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise failure

    monkeypatch.setattr(kctl_main, "profile_bootstrap_store_factory", lambda _profile: Store())

    assert (
        main(
            [
                "profile",
                "bootstrap",
                "create",
                "edge-gateway",
                "--site-id",
                "site-a",
                "--node-id",
                "gateway-a",
                "--transport-url",
                "tls://nats.example.test:4222",
                "--credential-file",
                "/private/credential",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "profile bootstrap failed closed"
    assert "DO-NOT-PRINT" not in captured.err
    assert "/private/" not in captured.err


def test_profile_migration_cli_routes_only_explicit_authority_and_redacts_paths(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    plan_digest = "sha256:" + "1" * 64
    receipt_digest = "sha256:" + "2" * 64
    migration_id = "m1-" + "3" * 32
    run_id = "r1-" + "4" * 32

    class Result:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def public_payload(self) -> dict[str, object]:
            return self.payload

    class Manager:
        def plan(self, legacy_profile, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("plan", {"legacy_profile": legacy_profile, **kwargs}))
            return Result(
                {
                    "apiVersion": "k1s.dev/v1alpha1",
                    "destinationProfile": "edge-dev",
                    "destinationStatus": "available",
                    "kind": "ProfileMigrationPlan",
                    "legacyProfile": "edge",
                    "migrationId": migration_id,
                    "mutating": False,
                    "planDigest": plan_digest,
                    "runId": run_id,
                    "schemaVersion": 1,
                }
            )

        def apply(self, legacy_profile, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("apply", {"legacy_profile": legacy_profile, **kwargs}))
            return Result(
                {
                    "action": "apply",
                    "apiVersion": "k1s.dev/v1alpha1",
                    "destinationProfile": "edge-dev",
                    "kind": "ProfileMigrationResult",
                    "legacyProfile": "edge",
                    "migrationId": migration_id,
                    "planDigest": plan_digest,
                    "receiptDigest": receipt_digest,
                    "runId": run_id,
                    "schemaVersion": 1,
                    "status": "applied",
                }
            )

        def rollback(self, legacy_profile, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(("rollback", {"legacy_profile": legacy_profile, **kwargs}))
            return Result(
                {
                    "action": "rollback",
                    "apiVersion": "k1s.dev/v1alpha1",
                    "destinationProfile": "edge-dev",
                    "kind": "ProfileMigrationResult",
                    "legacyProfile": "edge",
                    "migrationId": migration_id,
                    "planDigest": plan_digest,
                    "receiptDigest": receipt_digest,
                    "runId": run_id,
                    "schemaVersion": 1,
                    "status": "rolled-back",
                }
            )

    manager = Manager()
    monkeypatch.setattr(kctl_main, "profile_migration_manager_factory", lambda: manager)
    legacy_root = tmp_path / "private-legacy"
    destination_root = tmp_path / "private-state"

    common = [
        "edge",
        "--legacy-root",
        str(legacy_root),
        "--destination-profile",
        "edge-dev",
        "--destination-root",
        str(destination_root),
        "-o",
        "json",
    ]
    assert main(["profile", "migrate", "plan", *common]) == 0
    plan_output = capsys.readouterr().out
    assert (
        main(
            [
                "profile",
                "migrate",
                "apply",
                *common,
                "--expect-plan-digest",
                plan_digest,
            ]
        )
        == 0
    )
    apply_output = capsys.readouterr().out
    assert (
        main(
            [
                "profile",
                "migrate",
                "rollback",
                *common,
                "--migration-id",
                migration_id,
                "--expect-receipt-digest",
                receipt_digest,
                "--confirm-rollback",
                f"edge-dev/{run_id}",
            ]
        )
        == 0
    )
    rollback_output = capsys.readouterr().out

    assert [item[0] for item in calls] == ["plan", "apply", "rollback"]
    assert calls[0][1]["legacy_root"] == legacy_root
    assert calls[0][1]["destination_root"] == destination_root
    assert calls[1][1]["expected_plan_digest"] == plan_digest
    assert calls[2][1]["expected_receipt_digest"] == receipt_digest
    rendered = plan_output + apply_output + rollback_output
    assert str(legacy_root) not in rendered
    assert str(destination_root) not in rendered


@pytest.mark.parametrize(
    "failure",
    [
        kctl_main.ProfileStateError("DO-NOT-PRINT-/private/state"),
        OSError("DO-NOT-PRINT-/private/legacy"),
        kctl_main.ProfilePathError("DO-NOT-PRINT-/private/root"),
    ],
)
def test_profile_migration_cli_errors_are_generic(
    monkeypatch,
    capsys,
    tmp_path: Path,
    failure: Exception,
) -> None:
    class Manager:
        def plan(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise failure

    monkeypatch.setattr(kctl_main, "profile_migration_manager_factory", Manager)
    assert (
        main(
            [
                "profile",
                "migrate",
                "plan",
                "edge",
                "--legacy-root",
                str(tmp_path / "private-legacy"),
                "--destination-profile",
                "edge-dev",
                "--destination-root",
                str(tmp_path / "private-state"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "profile migration failed closed"
    assert "DO-NOT-PRINT" not in captured.err
    assert "/private/" not in captured.err


def test_canonical_node_lifecycle_routes_exact_bootstrap_without_fallback(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, str, str | None, str | None]] = []

    class Manager:
        def perform(  # type: ignore[no-untyped-def]
            self, action, profile, *, backend=None, bootstrap_id=None
        ):
            calls.append((action, profile, backend, bootstrap_id))
            return PortableLifecycleResult(
                action="start",
                profile="node",
                status="started",
                backend="podman",
                run_id="r1-" + "1" * 32,
                generation=1,
                phase="running",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", Manager)
    bootstrap_id = "b1-" + "2" * 32

    assert (
        main(
            [
                "profile",
                "start",
                "node",
                "--bootstrap-id",
                bootstrap_id,
                "-o",
                "json",
            ]
        )
        == 0
    )
    assert calls == [("start", "node", None, bootstrap_id)]
    assert json.loads(capsys.readouterr().out)["profile"] == "node"


def test_canonical_edge_start_restart_and_run_require_bootstrap_before_manager(
    monkeypatch,
    capsys,
) -> None:
    calls = 0

    def fail_factory():
        nonlocal calls
        calls += 1
        raise AssertionError("manager must not run without exact bootstrap identity")

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", fail_factory)

    for argv in (
        ["profile", "start", "edge-gateway"],
        ["profile", "restart", "node"],
        ["profile", "run", "edge-gateway"],
    ):
        assert main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip() == "profile command failed closed"
    assert calls == 0


def test_exact_bootstrap_routes_restart_and_foreground_run(
    monkeypatch,
    capsys,
) -> None:
    bootstrap_id = "b1-" + "3" * 32
    calls: list[tuple[str, str, str | None]] = []

    class Manager:
        def perform(  # type: ignore[no-untyped-def]
            self, action, profile, *, backend=None, bootstrap_id=None
        ):
            assert backend is None
            calls.append((action, profile, bootstrap_id))
            return PortableLifecycleResult(
                action="restart",
                profile="edge-gateway",
                status="restarted",
                backend="gateway",
                run_id="r1-" + "1" * 32,
                generation=2,
                phase="running",
                exit_code=0,
            )

        def run(  # type: ignore[no-untyped-def]
            self, profile, *, backend, bootstrap_id, stop_requested
        ):
            assert backend is None and stop_requested() is None
            calls.append(("run", profile, bootstrap_id))
            return PortableLifecycleResult(
                action="run",
                profile="node",
                status="externally-stopped",
                backend="podman",
                run_id="r1-" + "2" * 32,
                generation=1,
                phase="stopped",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", Manager)

    assert (
        main(
            [
                "profile",
                "restart",
                "edge-gateway",
                "--bootstrap-id",
                bootstrap_id,
                "-o",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "profile",
                "run",
                "node",
                "--bootstrap-id",
                bootstrap_id,
                "-o",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert calls == [
        ("restart", "edge-gateway", bootstrap_id),
        ("run", "node", bootstrap_id),
    ]


def test_host_prepare_cli_passes_only_explicit_authority_and_renders_public_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    class Manager:
        def prepare(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return HostTransactionResult(
                action="prepare",
                status="prepared",
                transaction_id="ht1-" + "1" * 32,
                target_id="guest-a",
                plan_digest="sha256:" + "a" * 64,
                receipt_digest="sha256:" + "b" * 64,
                applied_receipt_digest="sha256:" + "b" * 64,
            )

    monkeypatch.setattr(
        kctl_main,
        "profile_host_transaction_manager_factory",
        lambda request_fd, response_fd: (
            captured.update({"request_fd": request_fd, "response_fd": response_fd}) or Manager()
        ),
    )
    args = [
        "profile",
        "host",
        "prepare",
        "strict-cri",
        "--intent",
        str(tmp_path / "intent.json"),
        "--expect-intent-sha256",
        "sha256:" + "1" * 64,
        "--facts",
        str(tmp_path / "facts.json"),
        "--expect-facts-sha256",
        "sha256:" + "2" * 64,
        "--plan",
        str(tmp_path / "plan.json"),
        "--expect-plan-sha256",
        "sha256:" + "3" * 64,
        "--expect-plan-digest",
        "sha256:" + "a" * 64,
        "--transaction-root",
        str(tmp_path / "private"),
        "--transaction-id",
        "ht1-" + "1" * 32,
        "--helper-request-fd",
        "8",
        "--helper-response-fd",
        "9",
        "--expect-helper-identity-sha256",
        "sha256:" + "4" * 64,
        "--confirm-prepare",
        "guest-a:sha256:" + "a" * 64,
        "-o",
        "json",
    ]

    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "prepared"
    assert captured["request_fd"] == 8
    assert captured["response_fd"] == 9
    assert captured["intent_path"] == tmp_path / "intent.json"
    assert str(tmp_path) not in json.dumps(payload)


def test_host_transaction_cli_errors_are_generic_and_redacted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    private = str(tmp_path / "private-authority")

    class Manager:
        def rollback(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProfileHostTransactionError(private)

    monkeypatch.setattr(
        kctl_main,
        "profile_host_transaction_manager_factory",
        lambda _request_fd, _response_fd: Manager(),
    )
    assert (
        main(
            [
                "profile",
                "host",
                "rollback",
                "strict-cri",
                "--transaction-root",
                private,
                "--transaction-id",
                "ht1-" + "1" * 32,
                "--helper-request-fd",
                "8",
                "--helper-response-fd",
                "9",
                "--expect-helper-identity-sha256",
                "sha256:" + "4" * 64,
                "--expect-applied-receipt-sha256",
                "sha256:" + "5" * 64,
                "--confirm-rollback",
                "guest-a:confirmation",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip() == "strict CRI host rollback failed closed"
    assert private not in output.err


def test_strict_cri_node_cli_passes_paired_explicit_inputs_without_engine_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    bootstrap_id = "b1-" + "6" * 32
    captured: dict[str, object] = {}

    class Manager:
        def perform(self, action, profile, **kwargs):  # type: ignore[no-untyped-def]
            captured.update({"action": action, "profile": profile, **kwargs})
            return PortableLifecycleResult(
                action="start",
                profile="node",
                status="started",
                backend="strict-cri",
                run_id="r1-" + "7" * 32,
                generation=1,
                phase="running",
                exit_code=0,
            )

    monkeypatch.setattr(kctl_main, "portable_profile_manager_factory", Manager)
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    assert (
        main(
            [
                "profile",
                "start",
                "node",
                "--bootstrap-id",
                bootstrap_id,
                "--host-plan",
                str(plan),
                "--expect-host-plan-sha256",
                "sha256:" + "1" * 64,
                "--expect-host-plan-digest",
                "sha256:" + "2" * 64,
                "--host-receipt",
                str(receipt),
                "--expect-host-receipt-sha256",
                "sha256:" + "3" * 64,
                "-o",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    inputs = captured["strict_cri_inputs"]
    assert isinstance(inputs, StrictCRINodeInputs)
    assert inputs.plan_path == plan
    assert inputs.receipt_path == receipt
    assert captured["backend"] is None
