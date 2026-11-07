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
    build_execution_plan,
    validate_dependency_order,
)
from ae.kctl.profile_engine import EngineCapability
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import ComponentState, ProcessIdentity, ProfileStateStore, command_digest
from ae.kctl.profile_supervisor import StopResult

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
        self.stop_calls: list[int] = []

    def prepare_component(
        self,
        component_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
    ) -> tuple[ComponentState, Pending]:
        del cwd, environment
        pid = 101 + len(self.prepared)
        pending = Pending(pid)
        self.prepared.append(component_id)
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
    ) -> bool:
        assert identity.pid > 0
        assert timeout_seconds == 30.0
        self.calls.append(component.component_id)
        return self.results.pop(0)


def _manager(
    store: ProfileStateStore,
    *,
    engine: EngineProbe | None = None,
    supervisor: Supervisor | None = None,
    readiness: Readiness | None = None,
) -> tuple[PortableProfileManager, EngineProbe, Supervisor, Readiness]:
    actual_engine = engine or EngineProbe()
    actual_supervisor = supervisor or Supervisor()
    actual_readiness = readiness or Readiness()
    manager = PortableProfileManager(
        store_factory=lambda _profile: store,
        engine_probe=actual_engine,
        supervisor=actual_supervisor,  # type: ignore[arg-type]
        readiness=actual_readiness,
        python_executable=Path("/usr/bin/python3"),
    )
    return manager, actual_engine, actual_supervisor, actual_readiness


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
    ) -> bool:
        del identity, timeout_seconds
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
