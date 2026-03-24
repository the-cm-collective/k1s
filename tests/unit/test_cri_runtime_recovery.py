from __future__ import annotations

from types import SimpleNamespace

import pytest

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


class _FakeGrpcError((grpc.RpcError if grpc is not None else Exception)):  # type: ignore[misc]
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


def test_cri_runtime_detects_stale_pod_sandbox_error() -> None:
    if grpc is None:
        pytest.skip("grpc unavailable")
    runtime = CRIRuntime()

    stale = _FakeGrpcError(
        grpc.StatusCode.NOT_FOUND,
        "failed to get sandbox container task: no running task found: task abc not found: not found",
    )
    other = _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "transport unavailable")

    assert runtime._is_stale_pod_sandbox_error(stale) is True
    assert runtime._is_stale_pod_sandbox_error(other) is False


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
        lambda method, req: (
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
    monkeypatch.setattr(runtime, "_remove_pod_sandbox", lambda pod_id: removed.append(str(pod_id)))

    runtime._run_pod(manifest, "default/demo-rev1-0", 1)

    assert removed == ["pod-1"]
    assert created_for == ["pod-1", "pod-2"]


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
    monkeypatch.setattr(runtime, "_remove_pod_sandbox", lambda pod_id: removed.append(str(pod_id)))
    monkeypatch.setattr(
        runtime,
        "_run_pod",
        lambda manifest, replica_id, revision, *, node_id=None, attempt=0, sandbox_recovery_attempt=0: recreated.append(
            (replica_id, revision, node_id, attempt, sandbox_recovery_attempt)
        ),
    )

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
