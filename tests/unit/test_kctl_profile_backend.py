from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from ae.kctl.profile_backend import (
    ComponentPlan,
    PortableBackendError,
    PortableProfileManager,
    _active_python_executable,
    build_execution_plan,
    validate_dependency_order,
)
from ae.kctl.profile_bootstrap import ProfileBootstrapStore
from ae.kctl.profile_engine import EngineCapability
from ae.kctl.profile_node_adapter import StrictCRINodeAuthority, StrictCRINodeInputs
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import ComponentState, ProcessIdentity, ProfileStateStore, command_digest
from ae.kctl.profile_supervisor import ForegroundWaitResult, StopResult

NOW = "2026-11-07T12:00:00Z"


def _roots(tmp_path: Path) -> ProfileRoots:
    return ProfileRoots.from_environment(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        owner_uid=os.geteuid(),
    )


def _store(tmp_path: Path, profile: str = "local") -> ProfileStateStore:
    counter = 0

    def random_bytes(length: int) -> bytes:
        nonlocal counter
        counter += 1
        return bytes([counter]) * length

    return ProfileStateStore(
        _roots(tmp_path),
        profile,
        clock=lambda: NOW,
        random_bytes=random_bytes,
    )


def _capability(backend: str = "podman") -> EngineCapability:
    return EngineCapability(
        backend=backend,  # type: ignore[arg-type]
        executable=Path(f"/usr/bin/{backend}"),
        executable_device=1,
        executable_inode=2,
        version="1.2.3",
        rootless=backend == "podman",
    )


def _identity(pid: int) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        process_group_id=pid,
        session_id=pid,
        boot_id="11111111-2222-3333-4444-555555555555",
        start_time_ticks=pid * 10,
        executable_device=9,
        executable_inode=10,
    )


def test_active_python_executable_preserves_virtualenv_entry_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "python3.11"
    target.write_bytes(b"bounded-python-placeholder")
    target.chmod(0o755)
    virtualenv_python = tmp_path / "venv" / "bin" / "python"
    virtualenv_python.parent.mkdir(parents=True)
    virtualenv_python.symlink_to(target)
    monkeypatch.setattr("sys.executable", str(virtualenv_python))

    assert _active_python_executable() == virtualenv_python


class EngineProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe(self, backend: str) -> EngineCapability:
        self.calls.append(backend)
        return _capability(backend)


class Pending:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.activated = False
        self.cancelled = False

    def activate(self) -> None:
        self.activated = True

    def cancel(self) -> None:
        self.cancelled = True

    @property
    def blocked(self) -> bool:
        return not self.activated and not self.cancelled


class Supervisor:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.pending: list[Pending] = []
        self.environments: list[dict[str, str]] = []
        self.argv: list[tuple[str, ...]] = []
        self.stop_calls: list[int] = []
        self.foreground_result = ForegroundWaitResult("stop-requested", signal_number=2)

    def prepare_component(
        self,
        component_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> tuple[ComponentState, Pending]:
        del cwd
        pid = 101 + len(self.prepared)
        pending = Pending(pid)
        self.prepared.append(component_id)
        self.environments.append(dict(environment))
        self.argv.append(tuple(argv))
        self.pending.append(pending)
        return (
            ComponentState(
                component_id=component_id,
                command_digest=command_digest(argv),
                log_path=f"logs/{log_path.name}",
                status="starting",
                process_identity=_identity(pid),
            ),
            pending,
        )

    def stop_exact(self, identity: ProcessIdentity) -> StopResult:
        self.stop_calls.append(identity.pid)
        return StopResult("stopped")

    def classify_exact(self, identity: ProcessIdentity) -> str:
        assert identity.pid > 0
        return "running"

    def wait_foreground(self, run, *, state_reader, stop_requested):  # type: ignore[no-untyped-def]
        del run, state_reader
        requested = stop_requested()
        if requested is not None:
            return ForegroundWaitResult("stop-requested", signal_number=requested)
        return self.foreground_result


class Readiness:
    def __init__(self, results: Sequence[bool] = (True, True)) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def wait_ready(
        self,
        component: ComponentPlan,
        identity: ProcessIdentity,
        *,
        timeout_seconds: float,
        stop_requested=None,  # type: ignore[no-untyped-def]
    ) -> bool:
        assert identity.pid > 0
        assert timeout_seconds == 30.0
        self.calls.append(component.component_id)
        PortableProfileManager._raise_if_stop_requested(stop_requested)
        return self.results.pop(0)


def _manager(
    store: ProfileStateStore,
    *,
    engine: EngineProbe | None = None,
    supervisor: Supervisor | None = None,
    readiness: Readiness | None = None,
    bootstrap_store: ProfileBootstrapStore | None = None,
    node_adapter_factory=None,  # type: ignore[no-untyped-def]
) -> tuple[PortableProfileManager, EngineProbe, Supervisor, Readiness]:
    actual_engine = engine or EngineProbe()
    actual_supervisor = supervisor or Supervisor()
    actual_readiness = readiness or Readiness()
    manager = PortableProfileManager(
        store_factory=lambda _profile: store,
        bootstrap_store_factory=(
            None if bootstrap_store is None else lambda _profile: bootstrap_store
        ),
        engine_probe=actual_engine,
        supervisor=actual_supervisor,  # type: ignore[arg-type]
        readiness=actual_readiness,
        python_executable=Path("/usr/bin/python3"),
        node_adapter_factory=node_adapter_factory,
    )
    return manager, actual_engine, actual_supervisor, actual_readiness


def _credential(tmp_path: Path, name: str, payload: str) -> Path:
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def _bootstrap_store(
    tmp_path: Path,
    profile: str,
    *,
    byte: int = 91,
) -> ProfileBootstrapStore:
    return ProfileBootstrapStore(
        _roots(tmp_path),
        profile,
        clock=lambda: NOW,
        random_bytes=lambda length: bytes([byte]) * length,
    )


def test_local_start_is_dependency_ordered_gated_and_exactly_owned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager, engine, supervisor, readiness = _manager(store)

    result = manager.perform("start", "local")
    state = store.inspect()

    assert result.status == "started"
    assert result.backend == "podman"
    assert engine.calls == ["podman"]
    assert supervisor.prepared == ["controller", "node-local"]
    assert readiness.calls == ["controller", "node-local"]
    assert all(pending.activated for pending in supervisor.pending)
    assert state is not None and state.phase == "running"
    assert state.backend_id == "podman"
    assert state.plan_digest is not None
    assert [component.component_id for component in state.components] == [
        "controller",
        "node-local",
    ]


def test_put_component_failure_cancels_unowned_blocked_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    manager, _, supervisor, readiness = _manager(store)

    def fail_put_component(*_args: object, **_kwargs: object) -> ComponentState:
        raise RuntimeError("injected durable component write failure")

    monkeypatch.setattr(store, "put_component", fail_put_component)

    with pytest.raises(RuntimeError, match="injected durable component write failure"):
        manager.start("local", backend="podman")

    assert len(supervisor.pending) == 1
    pending = supervisor.pending[0]
    assert pending.cancelled is True
    assert pending.activated is False
    assert pending.blocked is False
    assert readiness.calls == []
    state = store.inspect()
    assert state is not None and state.phase == "stopped"
    assert state.components == ()


def test_stop_uses_reverse_dependency_order_and_never_probes_engine(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager, engine, supervisor, _ = _manager(store)
    manager.start("local", backend="podman")
    engine.calls.clear()

    result = manager.stop("local")
    state = store.inspect()

    assert result.status == "stopped"
    assert engine.calls == []
    assert supervisor.stop_calls == [102, 101]
    assert state is not None and state.phase == "stopped"
    assert state.cleanup_complete
    assert all(component.process_identity is None for component in state.components)


def test_status_absent_is_read_only_and_does_not_probe_or_create_xdg(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager, engine, supervisor, readiness = _manager(store)

    result = manager.status("local")

    assert result.status == "absent"
    assert engine.calls == []
    assert supervisor.prepared == []
    assert readiness.calls == []
    assert not store.layout.roots.state.exists()


def test_readiness_failure_rolls_back_exact_started_component(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager, _, supervisor, _ = _manager(store, readiness=Readiness((False,)))

    with pytest.raises(PortableBackendError, match="readiness failed"):
        manager.start("local", backend="podman")

    state = store.inspect()
    assert supervisor.stop_calls == [101]
    assert state is not None and state.phase == "stopped"
    assert state.failure is not None and state.failure.code == "portable-start-failed"


def test_explicit_docker_never_falls_back_to_podman(tmp_path: Path) -> None:
    store = _store(tmp_path, "core")
    manager, engine, supervisor, _ = _manager(store, readiness=Readiness((True,)))

    result = manager.start("core", backend="docker")

    assert result.backend == "docker"
    assert engine.calls == ["docker"]
    assert supervisor.prepared == ["controller"]


def test_foreground_run_claims_new_run_and_signal_cleanup_is_normal_flow(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "core")
    manager, engine, supervisor, _ = _manager(store, readiness=Readiness((True,)))
    checks = 0

    def request_after_activation() -> int | None:
        nonlocal checks
        checks += 1
        return 15 if checks >= 8 else None

    result = manager.run(
        "core",
        backend="podman",
        stop_requested=request_after_activation,
    )

    state = store.inspect()
    assert result.action == "run"
    assert result.status == "signal-stopped"
    assert result.exit_code == 143
    assert engine.calls == ["podman"]
    assert supervisor.stop_calls == [101]
    assert state is not None and state.phase == "stopped"


@pytest.mark.parametrize(
    ("request_at", "prepared_count", "pending_cancelled"),
    [(1, 0, False), (3, 1, True), (4, 1, True), (5, 1, False), (6, 1, False)],
)
def test_foreground_signal_safe_boundaries_cancel_or_stop_exactly(
    tmp_path: Path,
    request_at: int,
    prepared_count: int,
    pending_cancelled: bool,
) -> None:
    store = _store(tmp_path / str(request_at), "core")
    manager, engine, supervisor, readiness = _manager(store, readiness=Readiness((True,)))
    checks = 0

    def request() -> int | None:
        nonlocal checks
        checks += 1
        return 2 if checks >= request_at else None

    result = manager.run("core", backend="podman", stop_requested=request)

    assert result.exit_code == 130
    assert result.phase == "stopped"
    assert engine.calls == ["podman"]
    assert len(supervisor.prepared) == prepared_count
    if pending_cancelled:
        assert supervisor.pending[0].cancelled is True
        assert supervisor.pending[0].activated is False
    if request_at == 6:
        assert readiness.calls == ["controller"]


def test_post_claim_plan_failure_rolls_back_without_second_capability_probe(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "core")
    engine = EngineProbe()
    manager = PortableProfileManager(
        store_factory=lambda _profile: store,
        engine_probe=engine,
        supervisor=Supervisor(),  # type: ignore[arg-type]
        readiness=Readiness((True,)),
        python_executable=Path("relative-python"),
    )

    with pytest.raises(PortableBackendError, match="must be absolute"):
        manager.run("core", backend="podman", stop_requested=lambda: None)

    state = store.inspect()
    assert engine.calls == ["podman"]
    assert state is not None and state.phase == "stopped"


def test_foreground_external_stop_result_uses_final_exact_stopped_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "core")
    manager, _, supervisor, _ = _manager(store, readiness=Readiness((True,)))

    def external_stop(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        stopped = manager.stop("core")
        assert stopped.phase == "stopped"
        return ForegroundWaitResult("stopped")

    monkeypatch.setattr(supervisor, "wait_foreground", external_stop)
    result = manager.run("core", backend="podman", stop_requested=lambda: None)

    assert result.status == "externally-stopped"
    assert result.phase == "stopped"
    assert result.exit_code == 0


def test_foreground_immediate_external_stop_after_start_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, "core")
    manager, _, _, _ = _manager(store, readiness=Readiness((True,)))
    original_start = manager.start

    def start_then_stop(*args, **kwargs):  # type: ignore[no-untyped-def]
        started = original_start(*args, **kwargs)
        stopped = manager.stop("core")
        assert stopped.run_id == started.run_id
        assert stopped.phase == "stopped"
        return started

    monkeypatch.setattr(manager, "start", start_then_stop)
    result = manager.run("core", backend="podman", stop_requested=lambda: None)

    assert result.status == "externally-stopped"
    assert result.phase == "stopped"
    assert result.run_id == store.inspect().run_id  # type: ignore[union-attr]
    assert result.exit_code == 0


def test_foreground_run_never_attaches_to_an_existing_active_run(tmp_path: Path) -> None:
    store = _store(tmp_path, "core")
    manager, _, supervisor, _ = _manager(store, readiness=Readiness((True,)))
    manager.start("core", backend="podman")
    prepared = list(supervisor.prepared)

    with pytest.raises(Exception, match="not cleanly terminal"):
        manager.run("core", backend="podman", stop_requested=lambda: None)

    assert supervisor.prepared == prepared


def test_manager_reads_exact_logs_and_deletes_only_confirmed_stopped_run(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "core")
    manager, _, _, _ = _manager(store, readiness=Readiness((True,)))
    started = manager.start("core", backend="podman")
    assert started.run_id is not None
    log = store.layout.for_run(started.run_id).log_path("controller")
    log.write_text("alpha\nbeta\n", encoding="utf-8")
    log.chmod(0o600)

    snapshot = manager.logs("core", component="controller", run_id=None, tail=1)
    stopped = manager.stop("core")
    deleted = manager.delete(
        "core",
        run_id=started.run_id,
        confirmation=f"core/{started.run_id}",
    )

    assert snapshot.lines == ("beta",)
    assert stopped.status == "stopped"
    assert deleted.status == "deleted"
    assert manager.status("core").status == "deleted"


def test_identity_mismatch_is_fenced_orphaned_without_cleanup(tmp_path: Path) -> None:
    store = _store(tmp_path, "core")
    starter, _, _, _ = _manager(store, readiness=Readiness((True,)))
    starter.start("core", backend="podman")

    class OrphanSupervisor(Supervisor):
        def stop_exact(self, identity: ProcessIdentity) -> StopResult:
            self.stop_calls.append(identity.pid)
            return StopResult("orphaned")

    orphan = OrphanSupervisor()
    manager, engine, _, _ = _manager(store, supervisor=orphan)
    engine.calls.clear()

    with pytest.raises(PortableBackendError, match="did not reach stopped"):
        manager.stop("core")

    state = store.inspect()
    assert engine.calls == []
    assert orphan.stop_calls == [101]
    assert state is not None and state.phase == "orphaned"
    assert state.failure is not None
    assert state.failure.code == "component-identity-mismatch"
    assert state.components[0].process_identity is not None


def test_plan_contains_only_explicit_environment_and_loopback_targets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    capability = _capability()
    digest = capability.digest()
    # The durable digest is produced by the manager; use a manager start to
    # obtain the exact value, then inspect the pure plan.
    manager, _, _, _ = _manager(store)
    manager.start("local", backend="podman")
    run = store.inspect()
    assert run is not None

    plan = build_execution_plan(
        "local", capability, run, store, python_executable=Path("/usr/bin/python3")
    )

    assert digest != plan.digest
    assert [component.dependencies for component in plan.components] == [(), ("controller",)]
    for component in plan.components:
        environment = dict(component.child_environment)
        assert "HTTP_PROXY" not in environment
        assert "TOKEN" not in environment
        assert all(target.port in {9108, 9109, 9110} for target in component.readiness)


def test_dependency_cycle_unknown_or_out_of_order_fails_closed(tmp_path: Path) -> None:
    base = ComponentPlan(
        component_id="controller",
        dependencies=(),
        argv=("python",),
        child_environment=(),
        cwd=tmp_path,
        log_path=tmp_path / "controller.log",
        readiness=(),
    )
    with pytest.raises(PortableBackendError, match="cycle"):
        validate_dependency_order([replace(base, dependencies=("controller",))])
    with pytest.raises(PortableBackendError, match="unknown"):
        validate_dependency_order([replace(base, dependencies=("node",))])


class SimulatedCrash(BaseException):
    pass


class CrashAfterActivation:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def wait_ready(
        self,
        component: ComponentPlan,
        identity: ProcessIdentity,
        *,
        timeout_seconds: float,
        stop_requested=None,  # type: ignore[no-untyped-def]
    ) -> bool:
        del identity, timeout_seconds, stop_requested
        self.calls.append(component.component_id)
        raise SimulatedCrash


def test_start_recovers_exact_gated_component_after_coordinator_crash(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    crashing_supervisor = Supervisor()
    crashing = PortableProfileManager(
        store_factory=lambda _profile: store,
        engine_probe=EngineProbe(),
        supervisor=crashing_supervisor,  # type: ignore[arg-type]
        readiness=CrashAfterActivation(),
        python_executable=Path("/usr/bin/python3"),
    )

    with pytest.raises(SimulatedCrash):
        crashing.start("local", backend="podman")

    interrupted = store.inspect()
    assert interrupted is not None and interrupted.phase == "starting"
    assert [component.status for component in interrupted.components] == ["starting"]
    assert crashing_supervisor.pending[0].activated

    resumed_supervisor = Supervisor()
    resumed, _, _, readiness = _manager(
        store,
        supervisor=resumed_supervisor,
        readiness=Readiness((True, True)),
    )
    result = resumed.start("local", backend="podman")

    assert result.status == "started"
    assert readiness.calls == ["controller", "node-local"]
    assert resumed_supervisor.prepared == ["node-local"]
    recovered = store.inspect()
    assert recovered is not None and recovered.phase == "running"
    assert [component.status for component in recovered.components] == [
        "running",
        "running",
    ]


def test_gateway_start_is_gateway_only_without_engine_fallback(tmp_path: Path) -> None:
    state_store = _store(tmp_path, "edge-gateway")
    bootstrap_store = _bootstrap_store(tmp_path, "edge-gateway")
    credential = _credential(tmp_path, "gateway.creds", "gateway-private-credential")
    bootstrap = bootstrap_store.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=credential,
    )
    manager, engine, supervisor, readiness = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
    )

    result = manager.start(
        "edge-gateway",
        backend=None,
        bootstrap_id=bootstrap.bootstrap_id,
    )

    assert result.status == "started"
    assert result.backend == "gateway"
    assert engine.calls == []
    assert supervisor.prepared == ["gateway"]
    assert readiness.calls == ["gateway"]
    assert "--kind" in supervisor.argv[0]
    assert "gateway" in supervisor.argv[0]
    assert "node-agent" not in supervisor.argv[0]
    environment = supervisor.environments[0]
    assert environment["AE_NODE_ID"] == "site-a--gateway-a"
    assert environment["AE_NATS_CREDS"].endswith("/secrets/gateway-credentials")
    state = state_store.inspect()
    assert state is not None and state.bootstrap_id == bootstrap.bootstrap_id
    assert state.private_bootstrap_digest == bootstrap.private_record_digest


def test_node_start_uses_explicit_identity_v1_and_private_child_token(
    tmp_path: Path,
) -> None:
    state_store = _store(tmp_path, "node")
    bootstrap_store = _bootstrap_store(tmp_path, "node")
    bootstrap = bootstrap_store.create_node(
        node_id="node-a",
        controller_url="https://controller.example.test:9443",
        runtime_backend="podman",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=_credential(tmp_path, "node.token", "node-private-token"),
    )
    manager, engine, supervisor, readiness = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
    )

    result = manager.start("node", backend=None, bootstrap_id=bootstrap.bootstrap_id)

    assert result.status == "started"
    assert result.backend == "podman"
    assert engine.calls == ["podman"]
    assert supervisor.prepared == ["node-agent"]
    assert readiness.calls == ["node-agent"]
    command = supervisor.argv[0]
    assert command[command.index("--node-id") + 1] == "node-a"
    assert command[command.index("--kind") + 1] == "node-agent"
    environment = supervisor.environments[0]
    assert environment["AE_AGENT_TOKEN"].startswith("node-private")
    state = state_store.inspect()
    assert state is not None
    assert state.secret_receipts == ()
    assert list(state_store.layout.for_run(state.run_id).run_secrets.iterdir()) == []
    plan = build_execution_plan(
        "node",
        _capability(),
        state,
        state_store,
        python_executable=manager.python_executable,
        bootstrap=bootstrap,
        credential_path=None,
    )
    assert "node-private-token" not in repr(plan)
    assert bootstrap.private_record_digest not in repr(plan)
    assert str(bootstrap.path) not in repr(plan)


def test_strict_cri_node_uses_receipt_bound_adapter_without_engine_probe(
    tmp_path: Path,
) -> None:
    state_store = _store(tmp_path, "node")
    bootstrap_store = _bootstrap_store(tmp_path, "node")
    bootstrap = bootstrap_store.create_node(
        node_id="node-a",
        controller_url="https://controller.example.test:9443",
        runtime_backend="strict-cri",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=_credential(tmp_path, "strict-cri-token", "private-node-token"),
    )
    authority = StrictCRINodeAuthority(
        bootstrap_id=bootstrap.bootstrap_id,
        node_id=bootstrap.node_id,
        controller_url=bootstrap.controller_url or "",
        listen_host=bootstrap.listen_host or "",
        listen_port=bootstrap.listen_port or 0,
        advertise_endpoint=bootstrap.advertise_endpoint or "",
        cri_endpoint="unix:///run/containerd/containerd.sock",
        runtime_handler="runc",
        plan_digest="sha256:" + "a" * 64,
        receipt_digest="sha256:" + "b" * 64,
        execution_digest="sha256:" + "c" * 64,
        child_environment=(
            ("AE_AGENT_TOKEN", bootstrap.credential),
            ("AE_CONTROLLER_URL", bootstrap.controller_url or ""),
            ("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock"),
            ("AE_NODE_ID", bootstrap.node_id),
            ("AE_RUNTIME_BACKEND", "cri"),
            ("LC_ALL", "C.UTF-8"),
            ("PYTHONUNBUFFERED", "1"),
            ("TZ", "UTC"),
        ),
    )

    class Adapter:
        def resolve(self, record, *, inputs):  # type: ignore[no-untyped-def]
            assert record == bootstrap
            assert inputs.expected_plan_digest == authority.plan_digest
            return authority

    manager, engine, supervisor, readiness = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
        node_adapter_factory=lambda _store: Adapter(),
    )
    inputs = StrictCRINodeInputs(
        plan_path=tmp_path / "plan.json",
        expected_plan_file_digest="sha256:" + "d" * 64,
        expected_plan_digest=authority.plan_digest,
        receipt_path=tmp_path / "receipt.json",
        expected_receipt_digest=authority.receipt_digest,
    )

    result = manager.start(
        "node",
        backend=None,
        bootstrap_id=bootstrap.bootstrap_id,
        strict_cri_inputs=inputs,
    )

    assert result.backend == "strict-cri"
    assert engine.calls == []
    assert supervisor.prepared == ["node-agent"]
    assert readiness.calls == ["node-agent"]
    command = supervisor.argv[0]
    assert command[command.index("--engine-backend") + 1] == "strict-cri"
    assert command[command.index("--host-plan-digest") + 1] == authority.plan_digest
    assert command[command.index("--host-receipt-digest") + 1] == authority.receipt_digest
    assert dict(supervisor.environments[0])["AE_RUNTIME_BACKEND"] == "cri"
    state = state_store.inspect()
    assert state is not None
    assert state.host_plan_digest == authority.plan_digest
    assert state.host_receipt_digest == authority.receipt_digest
    assert state.secret_receipts == ()


def test_strict_cri_node_fails_closed_without_explicit_adapter_authority(
    tmp_path: Path,
) -> None:
    state_store = _store(tmp_path, "node")
    bootstrap_store = _bootstrap_store(tmp_path, "node")
    bootstrap = bootstrap_store.create_node(
        node_id="node-a",
        controller_url="https://controller.example.test:9443",
        runtime_backend="strict-cri",
        listen_host="127.0.0.1",
        listen_port=9109,
        advertise_endpoint="https://node-a.example.test:9109",
        credential_file=_credential(tmp_path, "strict-cri-token", "private-node-token"),
    )
    manager, engine, supervisor, readiness = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
    )

    with pytest.raises(PortableBackendError, match="adapter authority is required"):
        manager.start("node", backend=None, bootstrap_id=bootstrap.bootstrap_id)
    assert engine.calls == []
    assert supervisor.prepared == []
    assert readiness.calls == []


def test_gateway_secret_materialization_failure_rolls_back_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_store = _store(tmp_path, "edge-gateway")
    bootstrap_store = _bootstrap_store(tmp_path, "edge-gateway")
    bootstrap = bootstrap_store.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=_credential(tmp_path, "gateway.creds", "gateway-private-value"),
    )
    manager, _, supervisor, readiness = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
    )

    def fail_secret(*_args: object, **_kwargs: object):
        raise PortableBackendError("injected private materialization failure")

    monkeypatch.setattr(state_store, "create_secret", fail_secret)
    with pytest.raises(PortableBackendError, match="injected private materialization"):
        manager.start("edge-gateway", backend=None, bootstrap_id=bootstrap.bootstrap_id)

    state = state_store.inspect()
    assert state is not None and state.phase == "stopped"
    assert state.cleanup_complete is True
    assert state.secret_receipts == ()
    assert supervisor.prepared == []
    assert readiness.calls == []


def test_canonical_edge_execution_never_accepts_alias_or_implicit_bootstrap(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "node")
    manager, _, _, _ = _manager(store)

    with pytest.raises(PortableBackendError, match="profile is unsupported"):
        manager.perform("start", "edge-dev")
    with pytest.raises(PortableBackendError, match="requires bootstrap ID"):
        manager.start("node", backend=None)
    with pytest.raises(PortableBackendError, match="comes from bootstrap identity"):
        manager.start("node", backend="podman", bootstrap_id="b1-" + "1" * 32)


def test_edge_restart_requires_explicit_bootstrap_before_stop_and_accepts_exact_id(
    tmp_path: Path,
) -> None:
    state_store = _store(tmp_path, "edge-gateway")
    bootstrap_store = _bootstrap_store(tmp_path, "edge-gateway")
    bootstrap = bootstrap_store.create_edge_gateway(
        site_id="site-a",
        node_id="gateway-a",
        transport_url="tls://nats.example.test:4222",
        credential_file=_credential(tmp_path, "gateway.creds", "gateway-private-credential"),
    )
    manager, engine, _, _ = _manager(
        state_store,
        bootstrap_store=bootstrap_store,
    )
    manager.start("edge-gateway", backend=None, bootstrap_id=bootstrap.bootstrap_id)

    with pytest.raises(PortableBackendError, match="requires an explicit bootstrap ID"):
        manager.restart("edge-gateway", backend=None)
    still_running = state_store.inspect()
    assert still_running is not None and still_running.phase == "running"
    assert manager.supervisor.stop_calls == []  # type: ignore[attr-defined]

    restarted = manager.restart(
        "edge-gateway",
        backend=None,
        bootstrap_id=bootstrap.bootstrap_id,
    )

    assert restarted.status == "restarted"
    assert restarted.backend == "gateway"
    assert engine.calls == []
    state = state_store.inspect()
    assert state is not None and state.bootstrap_id == bootstrap.bootstrap_id
