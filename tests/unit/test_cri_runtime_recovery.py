from __future__ import annotations

from types import SimpleNamespace

import pytest

import ae.runtime.cri_runtime as cri_runtime_module
from ae.controller.spec import AppManifest
from ae.runtime.cri_runtime import CRIRuntime, _StalePodSandboxError, grpc


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


class _PB2:
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
    other = _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "transport unavailable")

    assert runtime._is_stale_pod_sandbox_error(stale) is True
    assert runtime._is_stale_pod_sandbox_error(stale_create) is True
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


def test_run_pod_recovers_from_reserved_sandbox_name_with_backoff(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    pod_ids = iter(["pod-3"])
    removed: list[str] = []
    created_for: list[str] = []
    sleeps: list[float] = []
    failures = iter(["pod-1", "pod-2"])

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
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

    assert removed == ["pod-1", "pod-2"]
    assert created_for == ["pod-3"]
    assert sleeps == [1.0, 2.0]


def test_run_pod_raises_reserved_sandbox_name_after_three_retries(monkeypatch) -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime(node_id="hub-1")
    manifest = _manifest()
    removed: list[str] = []
    sleeps: list[float] = []

    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    monkeypatch.setattr(runtime, "_port_mappings", lambda _manifest: ([], {}))
    monkeypatch.setattr(runtime, "_ensure_sidecars", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cri_runtime_module.time, "sleep", lambda seconds: sleeps.append(seconds))

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

    assert removed == ["pod-1", "pod-1", "pod-1"]
    assert sleeps == [1.0, 2.0, 4.0]


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
