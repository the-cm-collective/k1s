"""Portable, exact-owner local/core profile orchestration.

The module defines live boundaries but never invokes them on import.  Unit and
artifact tests inject fakes; real engine/process/socket use occurs only after
an operator explicitly invokes a mutating portable lifecycle command.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast

from ae.kctl.profile_engine import EngineCapability, EngineName, SelectedEngineProbe
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import (
    ComponentState,
    FailureRecord,
    ProcessIdentity,
    ProfileStateError,
    ProfileStateStore,
    ResourceReceipt,
    RunState,
    command_digest,
)
from ae.kctl.profile_supervisor import (
    LinuxProcInspector,
    ProfileSupervisor,
    SupervisorError,
    classify_identity,
)

PortableProfile = Literal["local", "core"]
PortableAction = Literal["start", "status", "stop", "restart"]
PORTABLE_PROFILES: tuple[PortableProfile, ...] = ("local", "core")
PORTABLE_ACTIONS: tuple[PortableAction, ...] = ("start", "status", "stop", "restart")
CONTROLLER_METRICS_PORT = 9108
NODE_PORT = 9109
AGENT_API_PORT = 9110
DEFAULT_READINESS_TIMEOUT_SECONDS = 30.0


class PortableBackendError(ProfileStateError):
    """A portable profile cannot be planned or reconciled safely."""


class EngineProbe(Protocol):
    def probe(self, backend: str) -> EngineCapability: ...


@dataclass(frozen=True, slots=True)
class ReadinessTarget:
    component_id: str
    port: int
    path: str
    expected_node_id: str | None = None

    def __post_init__(self) -> None:
        if self.component_id not in {"controller", "node-local"}:
            raise PortableBackendError("readiness component is unsupported")
        if not 1 <= self.port <= 65535:
            raise PortableBackendError("readiness port is invalid")
        if not self.path.startswith("/") or "\x00" in self.path or "//" in self.path:
            raise PortableBackendError("readiness path is not canonical")


@dataclass(frozen=True, slots=True)
class ComponentPlan:
    component_id: str
    dependencies: tuple[str, ...]
    argv: tuple[str, ...]
    child_environment: tuple[tuple[str, str], ...]
    cwd: Path
    log_path: Path
    readiness: tuple[ReadinessTarget, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    profile: PortableProfile
    requested_name: str
    backend: EngineName
    engine: EngineCapability
    digest: str
    components: tuple[ComponentPlan, ...]


class ReadinessProbe(Protocol):
    def wait_ready(
        self,
        component: ComponentPlan,
        identity: ProcessIdentity,
        *,
        timeout_seconds: float,
    ) -> bool: ...


class StoreFactory(Protocol):
    def __call__(self, profile: str) -> ProfileStateStore: ...


@dataclass(frozen=True, slots=True)
class PortableLifecycleResult:
    action: PortableAction
    profile: PortableProfile
    status: str
    backend: str | None
    run_id: str | None
    generation: int | None
    phase: str | None
    exit_code: int
    qualification_posture: str = "source-validated/unqualified"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def portable_plan_digest(profile: PortableProfile, capability: EngineCapability) -> str:
    components = ["controller"] if profile == "core" else ["controller", "node-local"]
    payload = {
        "apiVersion": "k1s.dev/v1alpha1",
        "backendIdentity": capability.identity_payload(),
        "components": components,
        "kind": "PortableProfileExecutionPlan",
        "ports": {
            "agentApi": AGENT_API_PORT if profile == "local" else None,
            "controllerMetrics": CONTROLLER_METRICS_PORT,
            "node": NODE_PORT if profile == "local" else None,
        },
        "profile": profile,
        "schemaVersion": 1,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _child_environment(
    capability: EngineCapability,
    *,
    run_data: Path,
    local: bool,
) -> dict[str, str]:
    environment = {
        "AE_NODE_ID": "k1s-local" if local else "k1s-core",
        "AE_RUNTIME_BACKEND": capability.backend,
        "AE_SPECS_DIR": str(run_data / "specs"),
        "AE_STATE_DB": str(run_data / "controller.db"),
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "TZ": "UTC",
    }
    if capability.backend == "podman":
        environment["AE_PODMAN_BIN"] = str(capability.executable)
    if local:
        environment.update(
            {
                "AE_AGENT_API_HOST": "127.0.0.1",
                "AE_AGENT_API_PORT": str(AGENT_API_PORT),
                "AE_AGENT_URL": f"http://127.0.0.1:{NODE_PORT}",
                "AE_CONTROLLER_URL": f"http://127.0.0.1:{AGENT_API_PORT}",
            }
        )
    return environment


def build_execution_plan(
    profile: str,
    capability: EngineCapability,
    run: RunState,
    store: ProfileStateStore,
    *,
    python_executable: Path | None = None,
) -> ExecutionPlan:
    """Construct one deterministic installed plan without touching a runtime."""

    if profile not in PORTABLE_PROFILES or run.profile != profile:
        raise PortableBackendError("portable execution is limited to local and core")
    if run.backend_id != capability.backend:
        raise PortableBackendError("run backend differs from the selected engine")
    canonical_profile = profile
    expected_digest = portable_plan_digest(canonical_profile, capability)
    if run.plan_digest != expected_digest:
        raise PortableBackendError("execution plan identity differs from durable state")
    executable = Path(sys.executable if python_executable is None else python_executable)
    if not executable.is_absolute():
        raise PortableBackendError("Python executable must be absolute")
    layout = store.layout.for_run(run.run_id)
    environment = _child_environment(
        capability,
        run_data=layout.run_data,
        local=canonical_profile == "local",
    )
    runner = (
        str(executable),
        "-m",
        "ae.kctl.profile_component_runner",
        "--engine-backend",
        capability.backend,
        "--engine-executable",
        str(capability.executable),
        "--engine-device",
        str(capability.executable_device),
        "--engine-inode",
        str(capability.executable_inode),
    )
    controller_args = (
        "--kind",
        "controller",
        "--",
        "--loop",
        "--specs",
        str(layout.run_data / "specs"),
        "--metrics-port",
        str(CONTROLLER_METRICS_PORT),
    )
    controller_targets = [ReadinessTarget("controller", CONTROLLER_METRICS_PORT, "/health")]
    if canonical_profile == "local":
        controller_targets.append(ReadinessTarget("controller", AGENT_API_PORT, "/readyz"))
    components: list[ComponentPlan] = [
        ComponentPlan(
            component_id="controller",
            dependencies=(),
            argv=(*runner, *controller_args),
            child_environment=tuple(sorted(environment.items())),
            cwd=layout.run_data,
            log_path=layout.log_path("controller"),
            readiness=tuple(controller_targets),
        )
    ]
    if canonical_profile == "local":
        node_args = (
            "--kind",
            "node-local",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(NODE_PORT),
            "--controller-url",
            f"http://127.0.0.1:{AGENT_API_PORT}",
            "--node-id",
            "k1s-local",
            "--runtime-backend",
            capability.backend,
        )
        components.append(
            ComponentPlan(
                component_id="node-local",
                dependencies=("controller",),
                argv=(*runner, *node_args),
                child_environment=tuple(sorted(environment.items())),
                cwd=layout.run_data,
                log_path=layout.log_path("node-local"),
                readiness=(
                    ReadinessTarget("node-local", NODE_PORT, "/healthz"),
                    ReadinessTarget(
                        "node-local",
                        AGENT_API_PORT,
                        "/v1/nodes",
                        expected_node_id="k1s-local",
                    ),
                ),
            )
        )
    validate_dependency_order(components)
    return ExecutionPlan(
        profile=canonical_profile,
        requested_name=run.requested_name,
        backend=capability.backend,
        engine=capability,
        digest=expected_digest,
        components=tuple(components),
    )


def validate_dependency_order(components: Sequence[ComponentPlan]) -> None:
    seen: set[str] = set()
    all_ids = [component.component_id for component in components]
    if len(all_ids) != len(set(all_ids)):
        raise PortableBackendError("portable plan contains duplicate components")
    for component in components:
        if component.component_id in component.dependencies:
            raise PortableBackendError("portable plan contains a dependency cycle")
        unknown = set(component.dependencies) - set(all_ids)
        if unknown:
            raise PortableBackendError("portable plan contains an unknown dependency")
        if any(dependency not in seen for dependency in component.dependencies):
            raise PortableBackendError("portable plan is not in deterministic dependency order")
        seen.add(component.component_id)


class LoopbackReadinessProbe:
    """Bounded loopback-only probe; constructed only for an explicit start."""

    def __init__(
        self,
        *,
        inspector: LinuxProcInspector,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        per_attempt_timeout_seconds: float = 1.0,
        poll_interval_seconds: float = 0.1,
        max_response_bytes: int = 64 * 1024,
    ) -> None:
        self.inspector = inspector
        self.monotonic = monotonic
        self.sleep = sleep
        self.per_attempt_timeout_seconds = per_attempt_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_response_bytes = max_response_bytes

    def wait_ready(
        self,
        component: ComponentPlan,
        identity: ProcessIdentity,
        *,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise PortableBackendError("readiness deadline is outside the safe range")
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            if classify_identity(identity, self.inspector.inspect(identity.pid)) != "running":
                return False
            if all(self._probe(target) for target in component.readiness):
                return (
                    classify_identity(identity, self.inspector.inspect(identity.pid)) == "running"
                )
            self.sleep(self.poll_interval_seconds)
        return False

    def _probe(self, target: ReadinessTarget) -> bool:
        connection = http.client.HTTPConnection(
            "127.0.0.1", target.port, timeout=self.per_attempt_timeout_seconds
        )
        try:
            connection.request("GET", target.path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            payload = response.read(self.max_response_bytes + 1)
            if response.status != 200 or len(payload) > self.max_response_bytes:
                return False
            if target.expected_node_id is None:
                return True
            value = json.loads(payload.decode("utf-8"))
            if type(value) is not dict or type(value.get("nodes")) is not list:
                return False
            return any(
                type(node) is dict and node.get("node_id") == target.expected_node_id
                for node in value["nodes"]
            )
        except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
            return False
        finally:
            connection.close()


class NoAmbientResourceCleaner:
    """Portable item 4 owns no inferred engine resource."""

    def cleanup_resource(self, receipt: ResourceReceipt) -> None:
        raise PortableBackendError(
            f"no portable cleanup handler is authorized for {receipt.cleanup_handler_id}"
        )


class PortableProfileManager:
    """Coordinate exact state and injected engine/process/readiness boundaries."""

    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        engine_probe: EngineProbe,
        supervisor: ProfileSupervisor,
        readiness: ReadinessProbe,
        python_executable: Path,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    ) -> None:
        self.store_factory = store_factory
        self.engine_probe = engine_probe
        self.supervisor = supervisor
        self.readiness = readiness
        self.python_executable = python_executable
        self.readiness_timeout_seconds = readiness_timeout_seconds

    @classmethod
    def system(cls) -> PortableProfileManager:
        import time

        roots = ProfileRoots.from_environment()
        inspector = LinuxProcInspector()
        supervisor = ProfileSupervisor.system()
        return cls(
            store_factory=lambda profile: ProfileStateStore(roots, profile),
            engine_probe=SelectedEngineProbe.system(),
            supervisor=supervisor,
            readiness=LoopbackReadinessProbe(
                inspector=inspector,
                monotonic=time.monotonic,
                sleep=time.sleep,
            ),
            python_executable=Path(sys.executable).resolve(strict=True),
        )

    def perform(
        self,
        action: str,
        profile: str,
        *,
        backend: str | None = None,
    ) -> PortableLifecycleResult:
        if action not in PORTABLE_ACTIONS:
            raise PortableBackendError("unsupported portable lifecycle action")
        if profile not in PORTABLE_PROFILES:
            raise PortableBackendError("portable execution is limited to local and core")
        selected_profile = profile
        selected_action = action
        if selected_action in {"status", "stop"} and backend is not None:
            raise PortableBackendError("status and stop use only the durable backend identity")
        if selected_action == "start":
            return self.start(selected_profile, backend=backend or "podman")
        if selected_action == "status":
            return self.status(selected_profile)
        if selected_action == "stop":
            return self.stop(selected_profile)
        return self.restart(selected_profile, backend=backend)

    def start(self, profile: PortableProfile, *, backend: str) -> PortableLifecycleResult:
        capability = self.engine_probe.probe(backend)
        store = self.store_factory(profile)
        store.reconcile_transaction()
        current = store.inspect()
        digest = portable_plan_digest(profile, capability)
        if current is not None and current.phase != "stopped":
            if current.backend_id != capability.backend or current.plan_digest != digest:
                raise PortableBackendError(
                    "current portable run differs from the selected engine plan"
                )
            if current.phase not in {"preparing", "starting", "running"}:
                raise PortableBackendError("current portable run requires an explicit stop")
            run = current
        else:
            run = store.begin_run(backend_id=capability.backend, plan_digest=digest)
        plan = build_execution_plan(
            profile,
            capability,
            run,
            store,
            python_executable=self.python_executable,
        )
        if run.phase == "running":
            try:
                self._validate_running_plan(run, plan)
            except Exception as exc:
                self._rollback_start(store, exc)
                raise
            return self._result("start", run, "running")
        run_layout = store.layout.for_run(run.run_id)
        run_layout.ensure_run()
        specs = run_layout.run_data / "specs"
        if not specs.exists():
            specs.mkdir(mode=0o700)
        elif not specs.is_dir() or specs.is_symlink():
            raise PortableBackendError("portable specs path is not a directory")
        if run.phase == "preparing":
            run = store.transition(run.run_id, "starting", expected_revision=run.revision)
        try:
            existing = {component.component_id: component for component in run.components}
            expected_ids = [component.component_id for component in plan.components]
            if list(existing) != expected_ids[: len(existing)]:
                raise PortableBackendError(
                    "recorded components differ from the deterministic plan prefix"
                )
            for component_plan in plan.components:
                recorded = existing.get(component_plan.component_id)
                if recorded is not None:
                    run = self._recover_component(store, run, component_plan, recorded)
                    continue
                component, pending = self.supervisor.prepare_component(
                    component_plan.component_id,
                    component_plan.argv,
                    cwd=component_plan.cwd,
                    environment=dict(component_plan.child_environment),
                    log_path=component_plan.log_path,
                )
                activated = False
                try:
                    run = store.put_component(
                        run.run_id,
                        component,
                        expected_revision=run.revision,
                    )
                    pending.activate()
                    activated = True
                except Exception:
                    if not activated:
                        try:
                            pending.cancel()
                        except Exception as cancel_error:
                            raise PortableBackendError(
                                "pending component could not be cancelled"
                            ) from cancel_error
                    raise
                if not self.readiness.wait_ready(
                    component_plan,
                    cast(ProcessIdentity, component.process_identity),
                    timeout_seconds=self.readiness_timeout_seconds,
                ):
                    raise PortableBackendError(
                        f"component readiness failed: {component_plan.component_id}"
                    )
                run = store.put_component(
                    run.run_id,
                    replace(component, status="running"),
                    expected_revision=run.revision,
                )
            run = store.transition(run.run_id, "running", expected_revision=run.revision)
        except Exception as exc:
            self._rollback_start(store, exc)
            raise
        return self._result("start", run, "started")

    def status(self, profile: PortableProfile) -> PortableLifecycleResult:
        state = self.store_factory(profile).inspect()
        if state is None:
            return PortableLifecycleResult(
                action="status",
                profile=profile,
                status="absent",
                backend=None,
                run_id=None,
                generation=None,
                phase=None,
                exit_code=0,
            )
        return self._result("status", state, state.phase)

    def stop(self, profile: PortableProfile) -> PortableLifecycleResult:
        store = self.store_factory(profile)
        store.reconcile_transaction()
        state = store.inspect()
        if state is None:
            return PortableLifecycleResult(
                action="stop",
                profile=profile,
                status="absent",
                backend=None,
                run_id=None,
                generation=None,
                phase=None,
                exit_code=0,
            )
        if state.phase == "stopped":
            return self._result("stop", state, "stopped")
        if state.phase != "stopping":
            state = store.transition(
                state.run_id,
                "stopping",
                expected_revision=state.revision,
                failure=state.failure,
            )
        order = ["controller"] if state.profile == "core" else ["controller", "node-local"]
        by_id = {component.component_id: component for component in state.components}
        for component_id in reversed(order):
            component = by_id.get(component_id)
            if component is None or component.process_identity is None:
                continue
            result = self.supervisor.stop_exact(component.process_identity)
            if result.disposition != "stopped":
                if result.disposition == "orphaned":
                    state = store.transition(
                        state.run_id,
                        "orphaned",
                        expected_revision=state.revision,
                        failure=FailureRecord(
                            code="component-identity-mismatch",
                            summary="Recorded component identity no longer matches.",
                        ),
                    )
                raise PortableBackendError(
                    f"component did not reach stopped: {component.component_id}"
                )
            state = store.put_component(
                state.run_id,
                replace(component, status="stopped", process_identity=None),
                expected_revision=state.revision,
            )
        state = store.cleanup_owned(state.run_id, NoAmbientResourceCleaner())
        state = store.settle_components(state.run_id, expected_revision=state.revision)
        state = store.transition(
            state.run_id,
            "stopped",
            expected_revision=state.revision,
            cleanup_complete=True,
        )
        return self._result("stop", state, "stopped")

    def restart(
        self,
        profile: PortableProfile,
        *,
        backend: str | None,
    ) -> PortableLifecycleResult:
        store = self.store_factory(profile)
        store.reconcile_transaction()
        previous = store.inspect()
        selected = backend or (previous.backend_id if previous is not None else None) or "podman"
        self.stop(profile)
        started = self.start(profile, backend=selected)
        return replace(started, action="restart", status="restarted")

    def _rollback_start(
        self,
        store: ProfileStateStore,
        cause: Exception,
    ) -> None:
        del cause
        current = store.inspect()
        if current is None or current.phase == "stopped":
            return
        if current.phase not in {"failed", "stopping", "orphaned"}:
            current = store.transition(
                current.run_id,
                "failed",
                expected_revision=current.revision,
                failure=FailureRecord(
                    code="portable-start-failed",
                    summary="Portable component start or readiness failed.",
                ),
            )
        try:
            self.stop(cast(PortableProfile, current.profile))
        except (PortableBackendError, ProfileStateError, SupervisorError):
            # Exact failure state and process identities are deliberately retained
            # for the operator; no ambient cleanup is attempted.
            return

    def _recover_component(
        self,
        store: ProfileStateStore,
        run: RunState,
        plan: ComponentPlan,
        component: ComponentState,
    ) -> RunState:
        if component.command_digest != command_digest(plan.argv):
            raise PortableBackendError(
                f"recorded component command differs: {component.component_id}"
            )
        identity = component.process_identity
        if identity is None or component.status not in {"starting", "running"}:
            raise PortableBackendError(
                f"recorded component is not recoverably active: {component.component_id}"
            )
        if self.supervisor.classify_exact(identity) != "running":
            raise PortableBackendError(
                f"recorded component identity is no longer exact: {component.component_id}"
            )
        if not self.readiness.wait_ready(
            plan,
            identity,
            timeout_seconds=self.readiness_timeout_seconds,
        ):
            raise PortableBackendError(f"component readiness failed: {component.component_id}")
        if component.status == "starting":
            return store.put_component(
                run.run_id,
                replace(component, status="running"),
                expected_revision=run.revision,
            )
        return run

    def _validate_running_plan(self, run: RunState, plan: ExecutionPlan) -> None:
        by_id = {component.component_id: component for component in run.components}
        if tuple(by_id) != tuple(component.component_id for component in plan.components):
            raise PortableBackendError("running components differ from the execution plan")
        for component_plan in plan.components:
            component = by_id[component_plan.component_id]
            if component.status != "running":
                raise PortableBackendError("running plan contains a non-running component")
            if component.command_digest != command_digest(component_plan.argv):
                raise PortableBackendError("running component command differs from the plan")
            identity = component.process_identity
            if identity is None or self.supervisor.classify_exact(identity) != "running":
                raise PortableBackendError("running component identity is no longer exact")
            if not self.readiness.wait_ready(
                component_plan,
                identity,
                timeout_seconds=self.readiness_timeout_seconds,
            ):
                raise PortableBackendError("running component is no longer ready")

    @staticmethod
    def _result(
        action: PortableAction,
        state: RunState,
        status: str,
    ) -> PortableLifecycleResult:
        return PortableLifecycleResult(
            action=action,
            profile=cast(PortableProfile, state.profile),
            status=status,
            backend=state.backend_id,
            run_id=state.run_id,
            generation=state.generation,
            phase=state.phase,
            exit_code=0,
        )


__all__ = [
    "AGENT_API_PORT",
    "CONTROLLER_METRICS_PORT",
    "ComponentPlan",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "EngineProbe",
    "ExecutionPlan",
    "LoopbackReadinessProbe",
    "NODE_PORT",
    "NoAmbientResourceCleaner",
    "PORTABLE_ACTIONS",
    "PORTABLE_PROFILES",
    "PortableBackendError",
    "PortableLifecycleResult",
    "PortableProfileManager",
    "ReadinessProbe",
    "ReadinessTarget",
    "build_execution_plan",
    "portable_plan_digest",
    "validate_dependency_order",
]
