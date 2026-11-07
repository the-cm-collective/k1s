"""Portable, exact-owner local/core profile orchestration.

The module defines live boundaries but never invokes them on import.  Unit and
artifact tests inject fakes; real engine/process/socket use occurs only after
an operator explicitly invokes a mutating portable lifecycle command.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import signal
import stat
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, cast

from ae.kctl.profile_bootstrap import (
    ProfileBootstrapRecord,
    ProfileBootstrapStore,
)
from ae.kctl.profile_engine import EngineCapability, SelectedEngineProbe
from ae.kctl.profile_paths import ProfileRoots
from ae.kctl.profile_state import (
    ComponentState,
    FailureRecord,
    ProcessIdentity,
    ProfileLogSnapshot,
    ProfileStateError,
    ProfileStateStore,
    ResourceReceipt,
    RunPhase,
    RunState,
    command_digest,
)
from ae.kctl.profile_supervisor import (
    LinuxProcInspector,
    ProfileSupervisor,
    SupervisorError,
    classify_identity,
)

PortableProfile = Literal["local", "core", "edge-gateway", "node"]
ExecutionBackend = Literal["podman", "docker", "gateway"]
PortableAction = Literal["start", "status", "stop", "restart", "run", "delete"]
PORTABLE_PROFILES: tuple[PortableProfile, ...] = (
    "local",
    "core",
    "edge-gateway",
    "node",
)
PORTABLE_ACTIONS: tuple[PortableAction, ...] = ("start", "status", "stop", "restart")
CONTROLLER_METRICS_PORT = 9108
NODE_PORT = 9109
AGENT_API_PORT = 9110
DEFAULT_READINESS_TIMEOUT_SECONDS = 30.0


class PortableBackendError(ProfileStateError):
    """A portable profile cannot be planned or reconciled safely."""


class _ForegroundStopRequested(PortableBackendError):
    def __init__(self, signal_number: int) -> None:
        super().__init__("foreground stop requested")
        self.signal_number = signal_number


class EngineProbe(Protocol):
    def probe(self, backend: str) -> EngineCapability: ...


@dataclass(frozen=True, slots=True)
class ReadinessTarget:
    component_id: str
    port: int
    path: str
    expected_node_id: str | None = None

    def __post_init__(self) -> None:
        if self.component_id not in {"controller", "node-local", "node-agent"}:
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
    child_environment: tuple[tuple[str, str], ...] = field(repr=False)
    cwd: Path
    log_path: Path
    readiness: tuple[ReadinessTarget, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    profile: PortableProfile
    requested_name: str
    backend: ExecutionBackend
    engine: EngineCapability | None
    digest: str
    components: tuple[ComponentPlan, ...]
    bootstrap_id: str | None = None


class ReadinessProbe(Protocol):
    def wait_ready(
        self,
        component: ComponentPlan,
        identity: ProcessIdentity,
        *,
        timeout_seconds: float,
        stop_requested: Callable[[], int | None] | None = None,
    ) -> bool: ...


class StoreFactory(Protocol):
    def __call__(self, profile: str) -> ProfileStateStore: ...


class BootstrapStoreFactory(Protocol):
    def __call__(self, profile: str) -> ProfileBootstrapStore: ...


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


def portable_plan_digest(
    profile: PortableProfile,
    capability: EngineCapability | None,
    *,
    bootstrap: ProfileBootstrapRecord | None = None,
    python_executable: Path | None = None,
) -> str:
    """Return a public digest that never contains a secret or secret verifier."""

    if profile in {"edge-gateway", "node"}:
        if bootstrap is None or bootstrap.profile != profile or python_executable is None:
            raise PortableBackendError("canonical edge/node plan requires exact bootstrap identity")
        if profile == "node" and capability is None:
            raise PortableBackendError("node plan requires exact engine identity")
        if profile == "edge-gateway" and capability is not None:
            raise PortableBackendError("gateway plan must not contain engine identity")
        components = ["gateway"] if profile == "edge-gateway" else ["node-agent"]
        payload = {
            "apiVersion": "k1s.dev/v1alpha1",
            "backendIdentity": (capability.identity_payload() if capability is not None else None),
            "bootstrap": bootstrap.public_projection(),
            "components": components,
            "kind": "PortableProfileExecutionPlan",
            "profile": profile,
            "pythonExecutableIdentity": _executable_identity(python_executable),
            "schemaVersion": 2,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    if capability is None:
        raise PortableBackendError("local/core plan requires exact engine identity")
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


def _executable_identity(path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise PortableBackendError("Python executable must be absolute")
    try:
        info = path.stat()
    except OSError as exc:
        raise PortableBackendError("Python executable identity is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_ino <= 0:
        raise PortableBackendError("Python executable identity is invalid")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "path": str(path),
    }


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
    capability: EngineCapability | None,
    run: RunState,
    store: ProfileStateStore,
    *,
    python_executable: Path | None = None,
    bootstrap: ProfileBootstrapRecord | None = None,
    credential_path: Path | None = None,
) -> ExecutionPlan:
    """Construct one deterministic installed plan without touching a runtime."""

    if profile not in PORTABLE_PROFILES or run.profile != profile:
        raise PortableBackendError("portable execution profile is unsupported")
    executable = Path(sys.executable if python_executable is None else python_executable)

    if profile in {"edge-gateway", "node"}:
        if bootstrap is None or bootstrap.profile != profile:
            raise PortableBackendError("portable plan bootstrap role differs")
        if run.bootstrap_id != bootstrap.bootstrap_id:
            raise PortableBackendError("portable plan bootstrap ID differs from durable state")
        if run.private_bootstrap_digest != bootstrap.private_record_digest:
            raise PortableBackendError("private bootstrap identity differs from durable state")
        backend = bootstrap.backend_id
        if run.backend_id != backend:
            raise PortableBackendError("run backend differs from the bootstrap record")
        if profile == "node":
            if capability is None or capability.backend != backend:
                raise PortableBackendError("node engine differs from the bootstrap record")
        elif capability is not None:
            raise PortableBackendError("gateway plan must not receive engine identity")
        expected_digest = portable_plan_digest(
            profile,
            capability,
            bootstrap=bootstrap,
            python_executable=executable,
        )
        if run.plan_digest != expected_digest:
            raise PortableBackendError("execution plan identity differs from durable state")
        if profile == "edge-gateway":
            if credential_path is None or not credential_path.is_absolute():
                raise PortableBackendError("private gateway credential was not materialized")
        elif credential_path is not None:
            raise PortableBackendError("node token must not be materialized as a run secret")
        runner: tuple[str, ...] = (
            str(executable),
            "-m",
            "ae.kctl.profile_component_runner",
        )
        if capability is not None:
            runner = (
                *runner,
                "--engine-backend",
                capability.backend,
                "--engine-executable",
                str(capability.executable),
                "--engine-device",
                str(capability.executable_device),
                "--engine-inode",
                str(capability.executable_inode),
            )
        layout = store.layout.for_run(run.run_id)
        if profile == "edge-gateway":
            assert bootstrap.site_id is not None
            assert bootstrap.transport_url is not None
            environment = {
                "AE_GATEWAY_KEEP_SPOOL": "1",
                "AE_GATEWAY_SPOOL_PATH": str(layout.run_data / "gateway-spool.db"),
                "AE_NATS_CREDS": str(credential_path),
                "AE_NATS_URL": bootstrap.transport_url,
                "AE_NODE_ID": bootstrap.node_id,
                "AE_SITE_ID": bootstrap.site_id,
                "AE_TRANSPORT_BACKEND": "nats-js",
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "TZ": "UTC",
            }
            edge_components = (
                ComponentPlan(
                    component_id="gateway",
                    dependencies=(),
                    argv=(
                        *runner,
                        "--kind",
                        "gateway",
                        "--",
                        "--site-id",
                        bootstrap.site_id,
                        "--node-id",
                        bootstrap.node_id,
                        "--nats-url",
                        bootstrap.transport_url,
                    ),
                    child_environment=tuple(sorted(environment.items())),
                    cwd=layout.run_data,
                    log_path=layout.log_path("gateway"),
                    readiness=(),
                ),
            )
        else:
            assert capability is not None
            assert bootstrap.controller_url is not None
            assert bootstrap.listen_host is not None
            assert bootstrap.listen_port is not None
            assert bootstrap.advertise_endpoint is not None
            environment = {
                "AE_AGENT_TOKEN": bootstrap.credential,
                "AE_CONTROLLER_URL": bootstrap.controller_url,
                "AE_NODE_ID": bootstrap.node_id,
                "AE_RUNTIME_BACKEND": capability.backend,
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "TZ": "UTC",
            }
            if capability.backend == "podman":
                environment["AE_PODMAN_BIN"] = str(capability.executable)
            edge_components = (
                ComponentPlan(
                    component_id="node-agent",
                    dependencies=(),
                    argv=(
                        *runner,
                        "--kind",
                        "node-agent",
                        "--",
                        "--host",
                        bootstrap.listen_host,
                        "--port",
                        str(bootstrap.listen_port),
                        "--controller-url",
                        bootstrap.controller_url,
                        "--node-id",
                        bootstrap.node_id,
                        "--runtime-backend",
                        capability.backend,
                        "--advertise-endpoint",
                        bootstrap.advertise_endpoint,
                    ),
                    child_environment=tuple(sorted(environment.items())),
                    cwd=layout.run_data,
                    log_path=layout.log_path("node-agent"),
                    readiness=(ReadinessTarget("node-agent", bootstrap.listen_port, "/healthz"),),
                ),
            )
        validate_dependency_order(edge_components)
        return ExecutionPlan(
            profile=profile,
            requested_name=run.requested_name,
            backend=backend,
            engine=capability,
            digest=expected_digest,
            components=edge_components,
            bootstrap_id=bootstrap.bootstrap_id,
        )

    if capability is None or run.backend_id != capability.backend:
        raise PortableBackendError("run backend differs from the selected engine")
    canonical_profile = profile
    expected_digest = portable_plan_digest(canonical_profile, capability)
    if run.plan_digest != expected_digest:
        raise PortableBackendError("execution plan identity differs from durable state")
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
        bootstrap_id=None,
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
        stop_requested: Callable[[], int | None] | None = None,
    ) -> bool:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise PortableBackendError("readiness deadline is outside the safe range")
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            PortableProfileManager._raise_if_stop_requested(stop_requested)
            if classify_identity(identity, self.inspector.inspect(identity.pid)) != "running":
                return False
            if all(self._probe(target) for target in component.readiness):
                PortableProfileManager._raise_if_stop_requested(stop_requested)
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


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    backend: ExecutionBackend
    capability: EngineCapability | None
    bootstrap: ProfileBootstrapRecord | None
    plan_digest: str
    private_bootstrap_digest: str | None


class PortableProfileManager:
    """Coordinate exact state and injected engine/process/readiness boundaries."""

    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        bootstrap_store_factory: BootstrapStoreFactory | None = None,
        engine_probe: EngineProbe,
        supervisor: ProfileSupervisor,
        readiness: ReadinessProbe,
        python_executable: Path,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    ) -> None:
        self.store_factory = store_factory
        self.bootstrap_store_factory = bootstrap_store_factory
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
            bootstrap_store_factory=lambda profile: ProfileBootstrapStore(roots, profile),
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
        bootstrap_id: str | None = None,
    ) -> PortableLifecycleResult:
        if action not in PORTABLE_ACTIONS:
            raise PortableBackendError("unsupported portable lifecycle action")
        if profile not in PORTABLE_PROFILES:
            raise PortableBackendError("portable execution profile is unsupported")
        selected_profile = profile
        selected_action = action
        if selected_action in {"status", "stop"} and (
            backend is not None or bootstrap_id is not None
        ):
            raise PortableBackendError("status and stop use only the durable execution identity")
        if selected_action == "start":
            return self.start(
                selected_profile,
                backend=backend,
                bootstrap_id=bootstrap_id,
            )
        if selected_action == "status":
            return self.status(selected_profile)
        if selected_action == "stop":
            return self.stop(selected_profile)
        return self.restart(selected_profile, backend=backend, bootstrap_id=bootstrap_id)

    def start(
        self,
        profile: PortableProfile,
        *,
        backend: str | None,
        bootstrap_id: str | None = None,
        _required_run_id: str | None = None,
        _stop_requested: Callable[[], int | None] | None = None,
        _capability: EngineCapability | None = None,
        _bootstrap: ProfileBootstrapRecord | None = None,
    ) -> PortableLifecycleResult:
        context = self._execution_context(
            profile,
            backend=backend,
            bootstrap_id=bootstrap_id,
            capability=_capability,
            bootstrap=_bootstrap,
        )
        capability = context.capability
        store = self.store_factory(profile)
        store.reconcile_transaction()
        current = store.inspect()
        if _required_run_id is not None and (current is None or current.run_id != _required_run_id):
            raise PortableBackendError("foreground run ownership changed before activation")
        if current is not None and current.phase != "stopped":
            if (
                current.backend_id != context.backend
                or current.plan_digest != context.plan_digest
                or current.bootstrap_id
                != (context.bootstrap.bootstrap_id if context.bootstrap is not None else None)
                or current.private_bootstrap_digest != context.private_bootstrap_digest
            ):
                raise PortableBackendError(
                    "current portable run differs from the selected exact plan"
                )
            if current.phase not in {"preparing", "starting", "running"}:
                raise PortableBackendError("current portable run requires an explicit stop")
            run = current
        else:
            run = store.begin_run(
                backend_id=context.backend,
                plan_digest=context.plan_digest,
                bootstrap_id=(
                    context.bootstrap.bootstrap_id if context.bootstrap is not None else None
                ),
                private_bootstrap_digest=context.private_bootstrap_digest,
            )
        try:
            credential_path: Path | None = None
            if context.bootstrap is not None:
                bootstrap_store = self._bootstrap_store(profile)
                bootstrap_store.revalidate(context.bootstrap)
                if context.bootstrap.profile == "edge-gateway":
                    run, credential_path = self._materialize_gateway_credential(
                        store,
                        run,
                        context.bootstrap,
                    )
            plan = build_execution_plan(
                profile,
                capability,
                run,
                store,
                python_executable=self.python_executable,
                bootstrap=context.bootstrap,
                credential_path=credential_path,
            )
            if run.phase == "running":
                self._validate_running_plan(run, plan)
                return self._result("start", run, "running")
            run_layout = store.layout.for_run(run.run_id)
            run_layout.ensure_run()
            if profile in {"local", "core"}:
                specs = run_layout.run_data / "specs"
                if not specs.exists():
                    specs.mkdir(mode=0o700)
                elif not specs.is_dir() or specs.is_symlink():
                    raise PortableBackendError("portable specs path is not a directory")
            if run.phase == "preparing":
                run = store.transition(run.run_id, "starting", expected_revision=run.revision)
            self._raise_if_stop_requested(_stop_requested)
            existing = {component.component_id: component for component in run.components}
            expected_ids = [component.component_id for component in plan.components]
            if list(existing) != expected_ids[: len(existing)]:
                raise PortableBackendError(
                    "recorded components differ from the deterministic plan prefix"
                )
            for component_plan in plan.components:
                self._raise_if_stop_requested(_stop_requested)
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
                    self._raise_if_stop_requested(_stop_requested)
                    run = store.put_component(
                        run.run_id,
                        component,
                        expected_revision=run.revision,
                    )
                    self._raise_if_stop_requested(_stop_requested)
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
                self._raise_if_stop_requested(_stop_requested)
                if not self.readiness.wait_ready(
                    component_plan,
                    cast(ProcessIdentity, component.process_identity),
                    timeout_seconds=self.readiness_timeout_seconds,
                    stop_requested=_stop_requested,
                ):
                    raise PortableBackendError(
                        f"component readiness failed: {component_plan.component_id}"
                    )
                self._raise_if_stop_requested(_stop_requested)
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

    def run(
        self,
        profile: PortableProfile,
        *,
        backend: str | None,
        bootstrap_id: str | None = None,
        stop_requested: Callable[[], int | None],
    ) -> PortableLifecycleResult:
        """Own one newly-created portable run until it stops or fails."""

        context = self._execution_context(
            profile,
            backend=backend,
            bootstrap_id=bootstrap_id,
        )
        store = self.store_factory(profile)
        store.reconcile_transaction()
        # begin_run performs the active-run test and claim under one exclusive lock.
        claimed = store.begin_run(
            backend_id=context.backend,
            plan_digest=context.plan_digest,
            bootstrap_id=(
                context.bootstrap.bootstrap_id if context.bootstrap is not None else None
            ),
            private_bootstrap_digest=context.private_bootstrap_digest,
        )
        try:
            started = self.start(
                profile,
                backend=backend,
                bootstrap_id=bootstrap_id,
                _required_run_id=claimed.run_id,
                _stop_requested=stop_requested,
                _capability=context.capability,
                _bootstrap=context.bootstrap,
            )
        except _ForegroundStopRequested as requested:
            stopped = self.stop(profile)
            exit_code = 130 if requested.signal_number == signal.SIGINT else 143
            return replace(
                stopped,
                action="run",
                status="signal-stopped",
                exit_code=exit_code,
            )
        except Exception as exc:
            self._rollback_start(store, exc)
            raise
        state = store.inspect()
        if state is None or state.run_id != claimed.run_id:
            raise PortableBackendError("foreground run did not retain exact run ownership")
        if state.phase == "stopped":
            return self._result("run", state, "externally-stopped")
        if state.phase not in {"running", "stopping"}:
            raise PortableBackendError("foreground run entered an unexpected durable phase")
        outcome = self.supervisor.wait_foreground(
            state,
            state_reader=store.inspect,
            stop_requested=stop_requested,
        )
        if outcome.disposition == "stop-requested":
            stopped = self.stop(profile)
            assert outcome.signal_number is not None
            exit_code = 130 if outcome.signal_number == signal.SIGINT else 143
            return replace(
                stopped,
                action="run",
                status="signal-stopped",
                exit_code=exit_code,
            )
        if outcome.disposition == "stopped":
            final = store.inspect()
            if final is None or final.run_id != claimed.run_id or final.phase != "stopped":
                raise PortableBackendError("foreground run stopped without exact final state")
            return self._result("run", final, "externally-stopped")

        current = store.inspect()
        if current is None or current.run_id != claimed.run_id:
            return replace(started, action="run", status="ownership-lost", exit_code=1)
        target_phase = "orphaned" if outcome.disposition == "orphaned" else "failed"
        if current.phase not in {"failed", "orphaned", "stopping", "stopped"}:
            current = store.transition(
                current.run_id,
                cast(RunPhase, target_phase),
                expected_revision=current.revision,
                failure=FailureRecord(
                    code=(
                        "process-identity-mismatch"
                        if target_phase == "orphaned"
                        else "component-exited"
                    ),
                    summary="A foreground component lost its exact process authority.",
                ),
            )
        with suppress(PortableBackendError, ProfileStateError, SupervisorError):
            self.stop(profile)
        return replace(
            started,
            action="run",
            status=target_phase,
            phase=current.phase,
            exit_code=1,
        )

    def logs(
        self,
        profile: PortableProfile,
        *,
        component: str,
        run_id: str | None,
        tail: int,
    ) -> ProfileLogSnapshot:
        store = self.store_factory(profile)
        selected_run_id = run_id
        if selected_run_id is None:
            current = store.inspect()
            if current is None:
                raise PortableBackendError("profile has no current run")
            selected_run_id = current.run_id
        return store.read_log_snapshot(selected_run_id, component, tail=tail)

    def delete(
        self,
        profile: PortableProfile,
        *,
        run_id: str,
        confirmation: str,
    ) -> PortableLifecycleResult:
        store = self.store_factory(profile)
        existing = store.deletion_record(run_id)
        record = store.delete_run(run_id, confirmation=confirmation)
        state = store.inspect_run(run_id)
        return PortableLifecycleResult(
            action="delete",
            profile=profile,
            status=(
                "already-deleted"
                if existing is not None and existing.status == "complete"
                else "deleted"
            ),
            backend=state.backend_id,
            run_id=record.run_id,
            generation=record.generation,
            phase=state.phase,
            exit_code=0,
        )

    def status(self, profile: PortableProfile) -> PortableLifecycleResult:
        store = self.store_factory(profile)
        state = store.inspect()
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
        deletion = store.deletion_record(state.run_id)
        return self._result(
            "status",
            state,
            "deleted" if deletion is not None and deletion.status == "complete" else state.phase,
        )

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
        order_by_profile = {
            "core": ["controller"],
            "local": ["controller", "node-local"],
            "edge-gateway": ["gateway"],
            "node": ["node-agent"],
        }
        order = order_by_profile.get(state.profile)
        if order is None:
            raise PortableBackendError("recorded portable profile is unsupported")
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
        bootstrap_id: str | None = None,
    ) -> PortableLifecycleResult:
        store = self.store_factory(profile)
        store.reconcile_transaction()
        previous = store.inspect()
        context: _ExecutionContext | None = None
        if profile in {"edge-gateway", "node"}:
            if bootstrap_id is None:
                raise PortableBackendError(
                    "canonical edge/node restart requires an explicit bootstrap ID"
                )
            context = self._execution_context(
                profile,
                backend=backend,
                bootstrap_id=bootstrap_id,
            )
            selected = None
        else:
            selected = (
                backend or (previous.backend_id if previous is not None else None) or "podman"
            )
        self.stop(profile)
        started = self.start(
            profile,
            backend=selected,
            bootstrap_id=bootstrap_id,
            _capability=None if context is None else context.capability,
            _bootstrap=None if context is None else context.bootstrap,
        )
        return replace(started, action="restart", status="restarted")

    def _execution_context(
        self,
        profile: PortableProfile,
        *,
        backend: str | None,
        bootstrap_id: str | None,
        capability: EngineCapability | None = None,
        bootstrap: ProfileBootstrapRecord | None = None,
    ) -> _ExecutionContext:
        if profile in {"local", "core"}:
            if bootstrap_id is not None or bootstrap is not None:
                raise PortableBackendError(
                    "local/core execution does not accept bootstrap identity"
                )
            selected = backend or "podman"
            local_capability = capability or self.engine_probe.probe(selected)
            if local_capability.backend != selected:
                raise PortableBackendError("selected engine capability differs from the request")
            return _ExecutionContext(
                backend=local_capability.backend,
                capability=local_capability,
                bootstrap=None,
                plan_digest=portable_plan_digest(profile, local_capability),
                private_bootstrap_digest=None,
            )

        if backend is not None:
            raise PortableBackendError("canonical edge/node backend comes from bootstrap identity")
        if bootstrap is None:
            if bootstrap_id is None:
                raise PortableBackendError("canonical edge/node execution requires bootstrap ID")
            bootstrap = self._bootstrap_store(profile).load(bootstrap_id)
        elif bootstrap_id is not None and bootstrap.bootstrap_id != bootstrap_id:
            raise PortableBackendError("bootstrap ID differs from the supplied private record")
        if bootstrap.profile != profile:
            raise PortableBackendError("bootstrap role differs from the requested profile")
        bootstrap_capability: EngineCapability | None
        if profile == "node":
            bootstrap_capability = capability or self.engine_probe.probe(bootstrap.backend_id)
            if bootstrap_capability.backend != bootstrap.backend_id:
                raise PortableBackendError("node engine capability differs from bootstrap")
        else:
            if capability is not None:
                raise PortableBackendError("gateway execution must not probe an engine")
            bootstrap_capability = None
        return _ExecutionContext(
            backend=bootstrap.backend_id,
            capability=bootstrap_capability,
            bootstrap=bootstrap,
            plan_digest=portable_plan_digest(
                profile,
                bootstrap_capability,
                bootstrap=bootstrap,
                python_executable=self.python_executable,
            ),
            private_bootstrap_digest=bootstrap.private_record_digest,
        )

    def _bootstrap_store(self, profile: PortableProfile) -> ProfileBootstrapStore:
        if self.bootstrap_store_factory is None:
            raise PortableBackendError("bootstrap store is unavailable")
        return self.bootstrap_store_factory(profile)

    @staticmethod
    def _materialize_gateway_credential(
        store: ProfileStateStore,
        run: RunState,
        bootstrap: ProfileBootstrapRecord,
    ) -> tuple[RunState, Path]:
        if bootstrap.profile != "edge-gateway":
            raise PortableBackendError("only gateway credentials use run-owned secret files")
        receipt_id = "gateway-credentials"
        payload = bootstrap.credential_bytes()
        matching = [receipt for receipt in run.secret_receipts if receipt.secret_id == receipt_id]
        if not matching:
            if run.phase != "preparing":
                raise PortableBackendError("active bootstrap run lacks exact secret receipt")
            run, _receipt = store.create_secret(
                run.run_id,
                receipt_id,
                payload,
                expected_revision=run.revision,
            )
        elif len(matching) != 1 or len(run.secret_receipts) != 1:
            raise PortableBackendError("bootstrap run contains unexpected secret receipts")
        path = store.verify_secret(run.run_id, receipt_id, payload)
        return run, path

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

    @staticmethod
    def _raise_if_stop_requested(
        stop_requested: Callable[[], int | None] | None,
    ) -> None:
        if stop_requested is None:
            return
        requested = stop_requested()
        if requested is None:
            return
        if requested not in {signal.SIGINT, signal.SIGTERM}:
            raise PortableBackendError("foreground stop request is unsupported")
        raise _ForegroundStopRequested(requested)

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
            stop_requested=None,
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
                stop_requested=None,
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
