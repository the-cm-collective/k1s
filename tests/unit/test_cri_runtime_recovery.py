from __future__ import annotations

from types import SimpleNamespace

import pytest

import ae.runtime.cri_runtime as cri_runtime_module
from ae.controller.spec import AppManifest
from ae.runtime.cri_runtime import (
    CRIRuntime,
    _ReservedContainerNameError,
    _StalePodSandboxError,
    grpc,
)


def _manifest() -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo", "namespace": "default"},
            "spec": {
                "image": "docker.io/library/demo-shell:latest",
                "replicas": 1,
            },
        }
    )


class _FakeGrpcError(grpc.RpcError if grpc is not None else Exception):  # type: ignore[misc]
    def __init__(self, code, details: str) -> None:
        super().__init__()
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        delay = float(seconds)
        self.sleeps.append(delay)
        self.now += delay


class _PB2:
    class PodSandboxFilter:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class ListPodSandboxRequest:
        def __init__(self, *, filter=None) -> None:
            self.filter = filter

    class PodSandboxStatusRequest:
        def __init__(self, *, pod_sandbox_id: str, verbose: bool) -> None:
            self.pod_sandbox_id = pod_sandbox_id
            self.verbose = verbose

    class ContainerFilter:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class ListContainersRequest:
        def __init__(self, *, filter=None) -> None:
            self.filter = filter

    class ContainerStatusRequest:
        def __init__(self, *, container_id: str, verbose: bool) -> None:
            self.container_id = container_id
            self.verbose = verbose

    class PodSandboxMetadata:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class PodSandboxConfig:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class RunPodSandboxRequest:
        def __init__(self, config) -> None:
            self.config = config
            self.runtime_handler = ""

    class CreateContainerRequest:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class StartContainerRequest:
        def __init__(self, *, container_id: str) -> None:
            self.container_id = container_id

    class StopContainerRequest:
        def __init__(self, *, container_id: str, timeout: int) -> None:
            self.container_id = container_id
            self.timeout = timeout

    class RemoveContainerRequest:
        def __init__(self, *, container_id: str) -> None:
            self.container_id = container_id


class _PodCleanupPB2(_PB2):
    class StopPodSandboxRequest:
        def __init__(self, *, pod_sandbox_id: str) -> None:
            self.pod_sandbox_id = pod_sandbox_id

    class RemovePodSandboxRequest:
        def __init__(self, *, pod_sandbox_id: str) -> None:
            self.pod_sandbox_id = pod_sandbox_id


def test_cri_runtime_detects_stale_pod_sandbox_error() -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime()

    stale = _FakeGrpcError(
        grpc.StatusCode.NOT_FOUND,
        (
            "failed to get sandbox container task: "
            "no running task found: task abc not found: not found"
        ),
    )
    stale_create = _FakeGrpcError(
        grpc.StatusCode.NOT_FOUND,
        'failed to find sandbox id "pod-1": not found',
    )
    stale_create_generic = _FakeGrpcError(
        grpc.StatusCode.NOT_FOUND,
        'sandbox "pod-1" not found: not found',
    )
    stale_not_running = _FakeGrpcError(
        grpc.StatusCode.UNKNOWN,
        'sandbox container "pod-1" is not running',
    )
    other = _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "transport unavailable")

    assert runtime._is_stale_pod_sandbox_error(stale) is True
    assert runtime._is_stale_pod_sandbox_error(stale_create) is True
    assert runtime._is_stale_pod_sandbox_error(stale_create_generic) is True
    assert runtime._is_stale_pod_sandbox_error(stale_not_running) is True
    assert runtime._is_stale_pod_sandbox_error(other) is False


def test_cri_runtime_detects_container_in_removing_state_error() -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime()

    removing = _FakeGrpcError(
        grpc.StatusCode.UNKNOWN,
        (
            'failed to set starting state for container "abc": '
            "container is in removing state, can't be started"
        ),
    )
    other = _FakeGrpcError(grpc.StatusCode.UNKNOWN, "transport unavailable")

    assert runtime._is_container_in_removing_state_error(removing) is True
    assert runtime._is_container_in_removing_state_error(other) is False


def test_cri_runtime_detects_container_in_starting_state_remove_error() -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime()

    starting = _FakeGrpcError(
        grpc.StatusCode.UNKNOWN,
        (
            'failed to set removing state for container "abc": '
            "container is in starting state, can't be removed"
        ),
    )
    other = _FakeGrpcError(grpc.StatusCode.UNKNOWN, "transport unavailable")

    assert runtime._is_container_in_starting_state_remove_error(starting) is True
    assert runtime._is_container_in_starting_state_remove_error(other) is False


def test_cri_runtime_detects_reserved_container_name_error_and_extracts_id() -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime()

    reserved = _FakeGrpcError(
        grpc.StatusCode.UNKNOWN,
        (
            'failed to reserve container name "main_demo-rev1-0_default_uid_0": '
            'name "main_demo-rev1-0_default_uid_0" is reserved for "ctr-1"'
        ),
    )
    other = _FakeGrpcError(grpc.StatusCode.UNKNOWN, "transport unavailable")

    assert runtime._is_reserved_container_name_error(reserved) is True
    assert runtime._reserved_container_id_from_error(reserved) == "ctr-1"
    assert runtime._is_reserved_container_name_error(other) is False
    assert runtime._reserved_container_id_from_error(other) is None


def test_cri_runtime_reports_original_grpc_import_error(monkeypatch) -> None:
    runtime = CRIRuntime()
    monkeypatch.setattr(cri_runtime_module, "grpc", None)
    monkeypatch.setattr(
        cri_runtime_module,
        "_grpc_import_error",
        ModuleNotFoundError("no module named grpc"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="ModuleNotFoundError: no module named grpc"):
        runtime._ensure_clients()


def test_run_pod_recovers_once_from_stale_sandbox(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod_ids = iter(["pod-1", "pod-2"])
    removed: list[str] = []
    created_for: list[str] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id=next(pod_ids))
            if method == "RunPodSandbox"
            else None
        ),
    )

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id == "pod-1":
            raise _StalePodSandboxError(pod_id, "stale sandbox")

    monkeypatch.setattr(runtime, "_create_main_container", _create)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1"]
    assert created_for == ["pod-1", "pod-2"]


def test_create_main_container_maps_removing_state_start_error_to_recoverable_pod_error(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "_pod_status", lambda _pod_id: None)

    def _runtime_call(method, req):
        if method == "CreateContainer":
            return SimpleNamespace(container_id="ctr-1")
        if method == "StartContainer":
            raise _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                (
                    f'failed to set starting state for container "{req.container_id}": '
                    "container is in removing state, can't be started"
                ),
            )
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    with pytest.raises(_StalePodSandboxError, match="container is in removing state"):
        runtime._create_main_container(manifest, "pod-1", "default/demo-rev1-0", 1)


def test_create_main_container_maps_missing_sandbox_create_error_to_recoverable_pod_error(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "_pod_status", lambda _pod_id: None)

    def _runtime_call(method, _req):
        if method == "CreateContainer":
            raise _FakeGrpcError(
                grpc.StatusCode.NOT_FOUND,
                'failed to find sandbox id "pod-1": not found',
            )
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    with pytest.raises(_StalePodSandboxError, match="failed to find sandbox id"):
        runtime._create_main_container(manifest, "pod-1", "default/demo-rev1-0", 1)


def test_create_main_container_maps_non_running_sandbox_create_error_to_recoverable_pod_error(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "_pod_status", lambda _pod_id: None)

    def _runtime_call(method, _req):
        if method == "CreateContainer":
            raise _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'sandbox container "pod-1" is not running',
            )
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    with pytest.raises(_StalePodSandboxError, match="is not running"):
        runtime._create_main_container(manifest, "pod-1", "default/demo-rev1-0", 1)


def test_create_main_container_maps_generic_missing_sandbox_create_error_to_recoverable_pod_error(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "_pod_status", lambda _pod_id: None)

    def _runtime_call(method, _req):
        if method == "CreateContainer":
            raise _FakeGrpcError(
                grpc.StatusCode.NOT_FOUND,
                'sandbox "pod-1" not found: not found',
            )
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    with pytest.raises(_StalePodSandboxError, match='sandbox "pod-1" not found'):
        runtime._create_main_container(manifest, "pod-1", "default/demo-rev1-0", 1)


def test_create_main_container_maps_reserved_name_error_to_reserved_container_error(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "_pod_status", lambda _pod_id: None)

    def _runtime_call(method, _req):
        if method == "CreateContainer":
            raise _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                (
                    'failed to reserve container name "main_demo-rev1-0_default_uid_0": '
                    'name "main_demo-rev1-0_default_uid_0" is reserved for "ctr-1"'
                ),
            )
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    with pytest.raises(_ReservedContainerNameError, match="reserved for") as excinfo:
        runtime._create_main_container(manifest, "pod-1", "default/demo-rev1-0", 1)

    assert excinfo.value.container_id == "ctr-1"
    assert excinfo.value.container_name == "main_demo-rev1-0_default_uid_0"


def test_run_pod_recovers_from_reserved_sandbox_name_with_backoff(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod_ids = iter(["pod-5"])
    removed: list[str] = []
    created_for: list[str] = []
    sleeps: list[float] = []
    failures = iter(["pod-1", "pod-2", "pod-3", "pod-4"])

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    def _runtime_call(method, _req):
        if method == "RunPodSandbox":
            reserved_for = next(failures, None)
            if reserved_for is not None:
                raise _FakeGrpcError(
                    grpc.StatusCode.UNKNOWN,
                    'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                    f'name "default/demo-rev1-0_default_abc_0" is reserved for "{reserved_for}"',
                )
            return SimpleNamespace(pod_sandbox_id=next(pod_ids))
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: created_for.append(str(_args[1])),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1", "pod-2", "pod-3", "pod-4"]
    assert created_for == ["pod-5"]
    assert sleeps == [0.5, 1.0, 0.5, 2.0, 0.5, 4.0, 0.5, 4.0]


def test_run_pod_raises_reserved_sandbox_name_after_recovery_deadline(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    clock = _FakeClock()
    removed: list[str] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(runtime, "RESERVED_NAME_RECOVERY_TIMEOUT_SECONDS", 11.0)
    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)

    def _runtime_call(method, _req):
        if method == "RunPodSandbox":
            raise _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-1"',
            )
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    with pytest.raises(_FakeGrpcError):
        runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1", "pod-1", "pod-1", "pod-1"]
    assert clock.sleeps == [0.5, 1.0, 0.5, 2.0, 0.5, 4.0, 0.5, 2.0]


def test_run_pod_reserved_sandbox_name_sweeps_all_replica_pods(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    created_for: list[str] = []
    removed: list[str] = []
    sleeps: list[float] = []
    failures = iter(["pod-1"])
    visible_pods = ["pod-2", "pod-3"]

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: list(visible_pods))

    def _runtime_call(method, _req):
        if method == "RunPodSandbox":
            reserved_for = next(failures, None)
            if reserved_for is not None:
                raise _FakeGrpcError(
                    grpc.StatusCode.UNKNOWN,
                    'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                    f'name "default/demo-rev1-0_default_abc_0" is reserved for "{reserved_for}"',
                )
            return SimpleNamespace(pod_sandbox_id="pod-4")
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    def _remove_pod_sandbox(pod_id, **_kwargs) -> None:
        pod_id_s = str(pod_id)
        removed.append(pod_id_s)
        if pod_id_s in visible_pods:
            visible_pods.remove(pod_id_s)

    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        _remove_pod_sandbox,
    )
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: created_for.append(str(_args[1])),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1", "pod-2", "pod-3"]
    assert created_for == ["pod-4"]
    assert sleeps == [0.5, 1.0]


def test_run_pod_reserved_sandbox_name_waits_for_name_release_before_retry(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    created_for: list[str] = []
    removed: list[str] = []
    sleeps: list[float] = []
    reserved_present = {"value": True}
    run_pod_results = iter(
        [
            _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-1"',
            ),
            "pod-2",
        ]
    )

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        if float(seconds) == 0.2:
            reserved_present["value"] = False

    monkeypatch.setattr(cri_runtime_module.time, "sleep", _sleep)

    def _runtime_call(method, _req):
        if method == "RunPodSandbox":
            next_result = next(run_pod_results)
            if isinstance(next_result, Exception):
                raise next_result
            return SimpleNamespace(pod_sandbox_id=next_result)
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )
    monkeypatch.setattr(
        runtime,
        "_pod_status",
        lambda pod_id: SimpleNamespace(id=pod_id) if reserved_present["value"] else None,
    )
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: created_for.append(str(_args[1])),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1"]
    assert created_for == ["pod-2"]
    assert sleeps == [0.5, 1.0, 0.2]


def test_run_pod_recovers_from_stale_sandbox_with_backoff(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod_ids = iter(["pod-1", "pod-2", "pod-3"])
    removed: list[str] = []
    created_for: list[str] = []
    sleeps: list[float] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id=next(pod_ids))
            if method == "RunPodSandbox"
            else None
        ),
    )
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id in {"pod-1", "pod-2"}:
            raise _StalePodSandboxError(pod_id, "stale sandbox")

    monkeypatch.setattr(runtime, "_create_main_container", _create)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1", "pod-2"]
    assert created_for == ["pod-1", "pod-2", "pod-3"]
    assert sleeps == [1.0, 2.0]


def test_run_pod_recovers_from_reserved_container_name_with_backoff(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod_ids = iter(["pod-1", "pod-2"])
    removed: list[str] = []
    created_for: list[str] = []
    cleaned: list[tuple[str | None, str | None, int]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id=next(pod_ids))
            if method == "RunPodSandbox"
            else None
        ),
    )

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id == "pod-1":
            raise _ReservedContainerNameError("ctr-1", "reserved container name")

    monkeypatch.setattr(runtime, "_create_main_container", _create)
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda replica_id, container_id, *, timeout=0, **_kwargs: (
            cleaned.append((replica_id, container_id, timeout)) or True
        ),
    )
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert cleaned == [("default/demo-rev1-0", "ctr-1", 10)]
    assert removed == ["pod-1"]
    assert created_for == ["pod-1", "pod-2"]
    assert sleeps == [0.5, 1.0]


def test_run_pod_reserved_container_name_waits_for_name_release_before_retry(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    created_for: list[str] = []
    cleaned: list[tuple[str | None, str | None, int]] = []
    removed: list[str] = []
    sleeps: list[float] = []
    reserved_present = {"value": True}
    pod_ids = iter(["pod-1", "pod-2"])

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])

    def _sleep(seconds: float) -> None:
        sleeps.append(float(seconds))
        if float(seconds) == 0.2:
            reserved_present["value"] = False

    monkeypatch.setattr(cri_runtime_module.time, "sleep", _sleep)
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id=next(pod_ids))
            if method == "RunPodSandbox"
            else None
        ),
    )

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id == "pod-1":
            raise _ReservedContainerNameError("ctr-1", "reserved container name")

    monkeypatch.setattr(runtime, "_create_main_container", _create)
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda replica_id, container_id, *, timeout=0, **_kwargs: (
            cleaned.append((replica_id, container_id, timeout)) or True
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda container_id: (
            SimpleNamespace(pod_sandbox_id="pod-9", labels={})
            if str(container_id) == "ctr-1" and reserved_present["value"]
            else None
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert cleaned == [("default/demo-rev1-0", "ctr-1", 10)]
    assert removed == ["pod-1", "pod-9"]
    assert created_for == ["pod-1", "pod-2"]
    assert sleeps == [0.5, 1.0, 0.2]


def test_run_pod_reserved_container_name_raises_after_release_deadline_with_debug_state(
    monkeypatch, caplog
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    clock = _FakeClock()
    removed: list[str] = []
    cleaned: list[tuple[str | None, str | None, int]] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(runtime, "RESERVED_NAME_RECOVERY_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id="pod-1") if method == "RunPodSandbox" else None
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _ReservedContainerNameError("ctr-1", "reserved container name")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda replica_id, container_id, *, timeout=0, **_kwargs: (
            cleaned.append((replica_id, container_id, timeout)) or True
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda container_id: (
            SimpleNamespace(pod_sandbox_id="pod-9", labels={})
            if str(container_id) == "ctr-1"
            else None
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(_ReservedContainerNameError, match="reserved container name"):
            runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert cleaned == [("default/demo-rev1-0", "ctr-1", 10)]
    assert removed == ["pod-1", "pod-9"]
    assert "waiting for name release" in caplog.text
    assert "current_state=" in caplog.text
    assert "reserved_container_id" in caplog.text


def test_run_pod_raises_reserved_container_name_when_cleanup_is_not_safe(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            SimpleNamespace(pod_sandbox_id="pod-1") if method == "RunPodSandbox" else None
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _ReservedContainerNameError("ctr-1", "reserved container name")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(_ReservedContainerNameError, match="reserved container name"):
        runtime._run_pod(manifest, "default/demo-rev1-0", 1)


def test_run_pod_reserved_container_name_recovers_after_hidden_sandbox_reservations(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    created_for: list[str] = []
    removed: list[str] = []
    cleaned: list[tuple[str | None, str | None, int]] = []
    sleeps: list[float] = []
    reserved_container_present = {"value": True}
    run_pod_results = iter(
        [
            "pod-1",
            _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-11"',
            ),
            _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-12"',
            ),
            _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-13"',
            ),
            _FakeGrpcError(
                grpc.StatusCode.UNKNOWN,
                'failed to reserve sandbox name "default/demo-rev1-0_default_abc_0": '
                'name "default/demo-rev1-0_default_abc_0" is reserved for "pod-14"',
            ),
            "pod-10",
        ]
    )

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: (
            (_ for _ in ()).throw(next_result)
            if isinstance((next_result := next(run_pod_results)), Exception)
            else SimpleNamespace(pod_sandbox_id=next_result)
        )
        if method == "RunPodSandbox"
        else None,
    )

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id == "pod-1":
            raise _ReservedContainerNameError(
                "ctr-1",
                "reserved container name",
                container_name="main_demo-rev1-0_default_uid_0",
            )

    monkeypatch.setattr(runtime, "_create_main_container", _create)
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda replica_id, container_id, *, timeout=0, **_kwargs: (
            cleaned.append((replica_id, container_id, timeout))
            or reserved_container_present.__setitem__("value", False)
            or True
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda container_id: (
            SimpleNamespace(pod_sandbox_id="pod-9", labels={})
            if str(container_id) == "ctr-1" and reserved_container_present["value"]
            else None
        ),
    )
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert cleaned == [("default/demo-rev1-0", "ctr-1", 10)]
    assert removed == ["pod-1", "pod-9", "pod-11", "pod-12", "pod-13", "pod-14"]
    assert created_for == ["pod-1", "pod-10"]
    assert sleeps == [0.5, 1.0, 0.5, 2.0, 0.5, 4.0, 0.5, 4.0, 0.5, 4.0]


def test_run_pod_recovers_from_stale_then_reserved_container_name_without_labels(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    created_for: list[str] = []
    removed: list[str] = []
    calls: list[tuple[str, str]] = []
    sleeps: list[float] = []
    pod_ids = iter(["pod-1", "pod-2", "pod-3"])
    reserved_container_present = {"value": True}

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(runtime, "_wait_for_container_absent", lambda *_args, **_kwargs: None)

    def _runtime_call(method, req):
        if method == "RunPodSandbox":
            return SimpleNamespace(pod_sandbox_id=next(pod_ids))
        if method in {"StopContainer", "RemoveContainer"}:
            calls.append((method, str(getattr(req, "container_id", "?"))))
            if method == "RemoveContainer":
                reserved_container_present["value"] = False
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda container_id: (
            SimpleNamespace(labels={})
            if str(container_id) == "ctr-1" and reserved_container_present["value"]
            else None
        ),
    )

    expected_name = runtime._expected_main_containerd_name(
        manifest,
        "pod-2",
        "default/demo-rev1-0",
        attempt=0,
    )
    assert expected_name is not None

    def _create(*_args, **_kwargs) -> None:
        pod_id = str(_args[1])
        created_for.append(pod_id)
        if pod_id == "pod-1":
            raise _StalePodSandboxError(
                "pod-1",
                (
                    'failed to set starting state for container "abc": '
                    "container is in removing state, can't be started"
                ),
            )
        if pod_id == "pod-2":
            raise _ReservedContainerNameError(
                "ctr-1",
                "reserved container name",
                container_name=expected_name,
            )

    monkeypatch.setattr(runtime, "_create_main_container", _create)

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1", "pod-2"]
    assert created_for == ["pod-1", "pod-2", "pod-3"]
    assert calls == [("StopContainer", "ctr-1"), ("RemoveContainer", "ctr-1")]
    assert sleeps == [1.0, 0.5, 0.5, 2.0]


def test_ensure_main_container_recovers_from_stale_sandbox(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod = SimpleNamespace(id="pod-1", labels={"ae.pod_name": "default/demo-rev1-0"})
    removed: list[str] = []
    recreated: list[tuple[str, int, str | None, int, int]] = []

    monkeypatch.setattr(runtime, "_find_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _StalePodSandboxError("pod-1", "stale sandbox")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )
    def _run_pod(
        _manifest,
        replica_id,
        revision,
        *,
        node_id=None,
        attempt=0,
        sandbox_recovery_attempt=0,
        reserved_name_recovery_deadline=None,
    ) -> None:
        recreated.append((replica_id, revision, node_id, attempt, sandbox_recovery_attempt))

    monkeypatch.setattr(runtime, "_run_pod", _run_pod)

    changed = runtime._ensure_main_container(
        manifest,
        pod,
        1,
        is_job=False,
        job_backoff_limit=None,
    )

    assert changed is True
    assert removed == ["pod-1"]
    assert recreated == [("default/demo-rev1-0", 1, "hub-1", 0, 1)]


def test_ensure_main_container_waits_for_created_container_to_reach_running(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod = SimpleNamespace(id="pod-1", labels={"ae.pod_name": "default/demo-rev1-0"})
    container = SimpleNamespace(id="ctr-1")
    clock = _FakeClock()
    statuses = iter(
        [
            SimpleNamespace(state="created", exit_code=None, labels={}),
            SimpleNamespace(state="created", exit_code=None, labels={}),
            SimpleNamespace(state="running", exit_code=None, labels={}),
        ]
    )
    runtime_calls: list[str] = []
    sidecars: list[str] = []

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(runtime, "_find_container", lambda *_args, **_kwargs: container)
    monkeypatch.setattr(runtime, "_container_status", lambda _container_id: next(statuses))
    monkeypatch.setattr(runtime, "_container_state_name", lambda status: str(status.state))
    monkeypatch.setattr(
        runtime,
        "_is_container_running",
        lambda status: str(getattr(status, "state", "")) == "running",
    )
    monkeypatch.setattr(runtime, "_runtime_call", lambda method, _req: runtime_calls.append(method))
    monkeypatch.setattr(
        runtime,
        "_ensure_sidecars",
        lambda *_args, **_kwargs: sidecars.append("default/demo-rev1-0"),
    )

    changed = runtime._ensure_main_container(
        manifest,
        pod,
        1,
        is_job=False,
        job_backoff_limit=None,
    )

    assert changed is False
    assert runtime_calls == []
    assert sidecars == ["default/demo-rev1-0"]
    assert clock.sleeps == [0.2]


def test_cleanup_reserved_container_refuses_mismatched_replica(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(
        runtime,
        "_container_status",
        lambda _container_id: SimpleNamespace(
            labels={
                runtime.POD_LABEL: "default/demo-rev1-1",
                runtime.CONTAINER_LABEL: "main",
            }
        ),
    )

    def _unexpected_runtime_call(method, _req):
        raise AssertionError(f"unexpected runtime call: {method}")

    monkeypatch.setattr(runtime, "_runtime_call", _unexpected_runtime_call)

    assert (
        runtime._cleanup_reserved_container_for_replica(
            "default/demo-rev1-0",
            "ctr-1",
            timeout=10,
            container_name="main_demo-rev1-1_default_uid_0",
            expected_container_name="main_demo-rev1-0_default_uid_0",
        )
        is False
    )


def test_cleanup_reserved_container_allows_matching_reserved_name_without_labels(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_container_status", lambda _container_id: SimpleNamespace(labels={}))
    monkeypatch.setattr(runtime, "_wait_for_container_absent", lambda *_args, **_kwargs: None)

    def _runtime_call(method, req):
        calls.append((method, str(getattr(req, "container_id", "?"))))
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    assert (
        runtime._cleanup_reserved_container_for_replica(
            "default/demo-rev1-0",
            "ctr-1",
            timeout=10,
            container_name="main_demo-rev1-0_default_uid_0",
            expected_container_name="main_demo-rev1-0_default_uid_0",
        )
        is True
    )
    assert calls == [("StopContainer", "ctr-1"), ("RemoveContainer", "ctr-1")]


def test_ensure_main_container_recovers_from_reserved_container_name(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod = SimpleNamespace(id="pod-1", labels={"ae.pod_name": "default/demo-rev1-0"})
    removed: list[str] = []
    recreated: list[tuple[str, int, str | None, int, int]] = []
    cleaned: list[tuple[str | None, str | None, int]] = []

    monkeypatch.setattr(runtime, "_find_container", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _ReservedContainerNameError("ctr-1", "reserved container name")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda replica_id, container_id, *, timeout=0, **_kwargs: (
            cleaned.append((replica_id, container_id, timeout)) or True
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, **_kwargs: removed.append(str(pod_id)),
    )

    def _run_pod(
        _manifest,
        replica_id,
        revision,
        *,
        node_id=None,
        attempt=0,
        sandbox_recovery_attempt=0,
        reserved_name_recovery_deadline=None,
    ) -> None:
        recreated.append((replica_id, revision, node_id, attempt, sandbox_recovery_attempt))

    monkeypatch.setattr(runtime, "_run_pod", _run_pod)

    changed = runtime._ensure_main_container(
        manifest,
        pod,
        1,
        is_job=False,
        job_backoff_limit=None,
    )

    assert changed is True
    assert cleaned == [("default/demo-rev1-0", "ctr-1", 10)]
    assert removed == ["pod-1"]
    assert recreated == [("default/demo-rev1-0", 1, "hub-1", 0, 1)]


def test_ensure_main_container_retries_remove_when_container_is_still_starting(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod = SimpleNamespace(id="pod-1", labels={"ae.pod_name": "default/demo-rev1-0"})
    container = SimpleNamespace(id="ctr-1")
    clock = _FakeClock()
    statuses = iter(
        [
            SimpleNamespace(state="exited", exit_code=1, labels={}),
            SimpleNamespace(state="created", exit_code=None, labels={}),
            SimpleNamespace(state="exited", exit_code=1, labels={}),
        ]
    )
    remove_attempts = {"count": 0}
    runtime_calls: list[tuple[str, str]] = []
    created_attempts: list[int] = []
    sidecars: list[str] = []

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_find_container", lambda *_args, **_kwargs: container)
    monkeypatch.setattr(runtime, "_container_status", lambda _container_id: next(statuses))
    monkeypatch.setattr(runtime, "_container_state_name", lambda status: str(status.state))
    monkeypatch.setattr(
        runtime,
        "_is_container_running",
        lambda status: str(getattr(status, "state", "")) == "running",
    )
    monkeypatch.setattr(runtime, "_wait_for_container_absent", lambda *_args, **_kwargs: None)

    def _runtime_call(method, req):
        container_id = str(getattr(req, "container_id", "?"))
        runtime_calls.append((method, container_id))
        if method == "RemoveContainer":
            remove_attempts["count"] += 1
            if remove_attempts["count"] == 1:
                raise _FakeGrpcError(
                    grpc.StatusCode.UNKNOWN,
                    (
                        f'failed to set removing state for container "{container_id}": '
                        "container is in starting state, can't be removed"
                    ),
                )
        return None

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)
    monkeypatch.setattr(
        runtime,
        "_create_main_container",
        lambda *_args, **kwargs: created_attempts.append(int(kwargs.get("attempt", 0))),
    )
    monkeypatch.setattr(
        runtime,
        "_ensure_sidecars",
        lambda *_args, **_kwargs: sidecars.append("default/demo-rev1-0"),
    )

    changed = runtime._ensure_main_container(
        manifest,
        pod,
        1,
        is_job=False,
        job_backoff_limit=None,
    )

    assert changed is True
    assert runtime_calls == [("RemoveContainer", "ctr-1"), ("RemoveContainer", "ctr-1")]
    assert created_attempts == [1]
    assert sidecars == ["default/demo-rev1-0"]
    assert clock.sleeps == [0.2, 0.2, 0.5]


def test_stop_and_remove_pod_uses_blocking_sandbox_cleanup(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = SimpleNamespace(spec=SimpleNamespace(termination_grace_period_seconds=21))
    pod = SimpleNamespace(id="pod-1", labels={runtime.REPLICA_LABEL: "default/demo-rev1-0"})
    removed: list[tuple[str, str | None, int]] = []

    monkeypatch.setattr(
        runtime,
        "_remove_pod_sandbox",
        lambda pod_id, *, replica_id=None, timeout=0: removed.append(
            (str(pod_id), None if replica_id is None else str(replica_id), int(timeout))
        ),
    )

    runtime._stop_and_remove_pod(manifest, pod)

    assert removed == [("pod-1", "default/demo-rev1-0", 21)]


def test_remove_pod_sandbox_raises_when_cleanup_leaves_pod_visible(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    clock = _FakeClock()
    pod = SimpleNamespace(id="pod-1", labels={runtime.REPLICA_LABEL: "default/demo-rev1-0"})

    monkeypatch.setattr(runtime, "_pb2", lambda: _PodCleanupPB2)
    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(runtime, "_list_pods", lambda: [pod])
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, _req: SimpleNamespace(containers=[]) if method == "ListContainers" else None,
    )

    with pytest.raises(
        RuntimeError, match="timed out waiting for CRI pod sandbox pod-1 to disappear after cleanup"
    ):
        runtime._remove_pod_sandbox("pod-1", replica_id="default/demo-rev1-0", timeout=1)


def test_wait_for_pod_sandbox_absent_retries_after_list_pods_error(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    clock = _FakeClock()
    pod = SimpleNamespace(id="pod-1", labels={runtime.REPLICA_LABEL: "default/demo-rev1-0"})
    responses = iter([RuntimeError("transient list failure"), [pod], []])

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)

    def list_pods():
        current = next(responses)
        if isinstance(current, Exception):
            raise current
        return current

    monkeypatch.setattr(runtime, "_list_pods", list_pods)

    assert runtime._wait_for_pod_sandbox_absent("pod-1", timeout=1) is True
    assert clock.sleeps == [0.2, 0.2]


def test_wait_for_pod_sandbox_name_available_retries_after_list_pods_error(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    clock = _FakeClock()
    pod = SimpleNamespace(id="pod-1", labels={runtime.REPLICA_LABEL: "default/demo-rev1-0"})
    responses = iter([RuntimeError("transient list failure"), [pod], []])

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)

    def list_pods():
        current = next(responses)
        if isinstance(current, Exception):
            raise current
        return current

    monkeypatch.setattr(runtime, "_list_pods", list_pods)

    assert runtime._wait_for_pod_sandbox_name_available("default/demo-rev1-0", timeout=1) is True
    assert clock.sleeps == [0.2, 0.2]


def test_cleanup_replica_pod_sandboxes_raises_when_name_stays_reserved(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    clock = _FakeClock()
    pod = SimpleNamespace(id="pod-1", labels={runtime.REPLICA_LABEL: "default/demo-rev1-0"})

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(runtime, "_pod_ids_for_replica", lambda _replica_id: [])
    monkeypatch.setattr(runtime, "_list_pods", lambda: [pod])

    with pytest.raises(
        RuntimeError,
        match=(
            "timed out waiting for CRI pod sandbox name release for replica "
            "default/demo-rev1-0 after cleanup"
        ),
    ):
        runtime._cleanup_replica_pod_sandboxes("default/demo-rev1-0", timeout=1)


def test_prepare_pod_sandbox_recovery_skips_name_release_wait_for_reserved_name_recovery(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    cleanup_calls: list[tuple[str, bool, list[str] | None, int]] = []

    monkeypatch.setattr(cri_runtime_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runtime,
        "_cleanup_replica_pod_sandboxes",
        lambda replica_id, *, timeout=0, extra_pod_ids=None, wait_for_name_release=True: cleanup_calls.append(
            (
                replica_id,
                bool(wait_for_name_release),
                None if extra_pod_ids is None else list(extra_pod_ids),
                int(timeout),
            )
        ),
    )

    runtime._prepare_pod_sandbox_recovery(
        manifest,
        replica_id="default/demo-rev1-0",
        pod_id="pod-stale",
        sandbox_recovery_attempt=0,
        cause=RuntimeError("reserved sandbox name"),
        reason="CRI sandbox name still reserved",
        sweep_replica_pods=True,
        recovery_deadline=30.0,
    )

    assert cleanup_calls == [("default/demo-rev1-0", False, ["pod-stale"], 10)]


def test_wait_for_reserved_name_release_allows_reappeared_replica_resources(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    snapshot = {
        "replica_id": "default/demo-rev1-0",
        "reserved_pod_id": "pod-stale",
        "reserved_pod_present": False,
        "reserved_container_id": "ctr-stale",
        "reserved_container_present": False,
        "reserved_container_name": "main_demo-rev1-0_default_uid_0",
        "expected_container_name": "main_demo-rev1-0_default_uid_0",
        "visible_pod_ids": ["pod-new"],
        "visible_main_container_ids": ["ctr-new"],
        "pods": [],
        "containers": [],
    }

    monkeypatch.setattr(
        runtime,
        "_reserved_name_recovery_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    result = runtime._wait_for_reserved_name_release(
        replica_id="default/demo-rev1-0",
        reason="CRI container name still reserved",
        cause=RuntimeError("reserved container name"),
        cleanup_result="reserved container cleanup complete",
        recovery_deadline=10.0,
        reserved_pod_id="pod-stale",
        reserved_container_id="ctr-stale",
        reserved_container_name="main_demo-rev1-0_default_uid_0",
        expected_container_name="main_demo-rev1-0_default_uid_0",
        allow_reappeared_replica_resources=True,
    )

    assert result == snapshot


def test_reserved_container_name_recovery_skips_run_pod_when_replacement_reappears(
    monkeypatch,
) -> None:
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    run_calls: list[tuple[str, int]] = []
    snapshot = {
        "visible_pod_ids": ["pod-new"],
        "visible_main_container_ids": ["ctr-new"],
    }

    monkeypatch.setattr(runtime, "_container_status", lambda _container_id: None)
    monkeypatch.setattr(
        runtime,
        "_cleanup_reserved_container_for_replica",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        runtime,
        "_prepare_pod_sandbox_recovery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "_wait_for_reserved_name_release",
        lambda **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        runtime,
        "_run_pod",
        lambda _manifest, replica_id, revision, **_kwargs: run_calls.append((replica_id, revision)),
    )

    runtime._recover_from_reserved_container_name(
        manifest,
        pod_id="pod-current",
        reserved_container_id="ctr-stale",
        replica_id="default/demo-rev1-0",
        revision=1,
        attempt=0,
        sandbox_recovery_attempt=0,
        cause=_ReservedContainerNameError("ctr-stale", "reserved container name"),
        node_id="hub-1",
    )

    assert run_calls == []


def test_reserved_pod_sandbox_name_recovery_skips_run_pod_when_replacement_reappears(
    monkeypatch,
) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    run_calls: list[tuple[str, int]] = []
    snapshot = {
        "visible_pod_ids": ["pod-new"],
        "visible_main_container_ids": ["ctr-new"],
    }
    cause = _FakeGrpcError(
        grpc.StatusCode.UNKNOWN,
        'failed to reserve sandbox name "default/demo-rev1-0_default_uid_0": '
        'name "default/demo-rev1-0_default_uid_0" is reserved for "pod-stale"',
    )

    monkeypatch.setattr(
        runtime,
        "_prepare_pod_sandbox_recovery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime,
        "_wait_for_reserved_name_release",
        lambda **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        runtime,
        "_run_pod",
        lambda _manifest, replica_id, revision, **_kwargs: run_calls.append((replica_id, revision)),
    )

    runtime._recover_from_reserved_pod_sandbox_name(
        manifest,
        replica_id="default/demo-rev1-0",
        revision=1,
        attempt=0,
        sandbox_recovery_attempt=0,
        cause=cause,
        node_id="hub-1",
    )

    assert run_calls == []
