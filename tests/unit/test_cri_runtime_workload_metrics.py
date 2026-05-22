from __future__ import annotations

from types import SimpleNamespace

from ae.runtime import cri_runtime as cri_runtime_module
from ae.runtime.cri_runtime import CRIRuntime


class _PB2:
    class PodSandboxState:
        SANDBOX_READY = "ready"

    class PodSandboxStateValue:
        def __init__(self, *, state):
            self.state = state

    class PodSandboxFilter:
        def __init__(self, *, label_selector=None, state=None):
            self.label_selector = label_selector or {}
            self.state = state

    class ListPodSandboxRequest:
        def __init__(self, *, filter=None):
            self.filter = filter

    class PodSandboxStatsRequest:
        def __init__(self, *, pod_sandbox_id):
            self.pod_sandbox_id = pod_sandbox_id


def _pod(*, pod_id: str, app_name: str | None = None):
    labels = {"ae.app": app_name} if app_name else {}
    return SimpleNamespace(id=pod_id, labels=labels)


def _install_fake_grpc(monkeypatch):
    class _FakeRpcErrorBase(Exception):
        pass

    fake_grpc = SimpleNamespace(
        RpcError=_FakeRpcErrorBase,
        StatusCode=SimpleNamespace(
            UNKNOWN="unknown",
            NOT_FOUND="not_found",
            FAILED_PRECONDITION="failed_precondition",
        ),
    )
    monkeypatch.setattr(cri_runtime_module, "grpc", fake_grpc)
    return fake_grpc


def _stats(
    *,
    pod_id: str,
    app_name: str,
    timestamp: int,
    usage_nano_cores: int | None = None,
    usage_core_nano_seconds: int | None = None,
    memory_bytes: int = 0,
):
    return SimpleNamespace(
        attributes=SimpleNamespace(id=pod_id, labels={"ae.app": app_name}),
        cpu=SimpleNamespace(
            timestamp=timestamp,
            usage=SimpleNamespace(
                usage_nano_cores=usage_nano_cores,
                usage_core_nano_seconds=usage_core_nano_seconds,
            ),
        ),
        memory=SimpleNamespace(working_set_bytes=memory_bytes),
    )


def test_cri_runtime_list_workload_metrics_aggregates_per_app(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="node-a")
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)
    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)

    def _runtime_call(method, req):
        if method == "ListPodSandbox":
            assert req.filter.state.state == _PB2.PodSandboxState.SANDBOX_READY
            return SimpleNamespace(
                items=[
                    _pod(pod_id="pod-1", app_name="default/demo"),
                    _pod(pod_id="pod-2", app_name="default/demo"),
                ]
            )
        assert method == "PodSandboxStats"
        if req.pod_sandbox_id == "pod-1":
            return SimpleNamespace(
                stats=_stats(
                    pod_id="pod-1",
                    app_name="default/demo",
                    timestamp=2_000_000_000,
                    usage_nano_cores=500_000_000,
                    memory_bytes=134217728,
                )
            )
        return SimpleNamespace(
            stats=_stats(
                pod_id="pod-2",
                app_name="default/demo",
                timestamp=2_000_000_000,
                usage_nano_cores=250_000_000,
                memory_bytes=67108864,
            )
        )

    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        _runtime_call,
    )

    samples = runtime.list_workload_metrics()

    assert len(samples) == 1
    sample = samples[0]
    assert sample.app_name == "default/demo"
    assert sample.node_id == "node-a"
    assert sample.cpu_cores == 0.75
    assert sample.memory_bytes == 201326592
    assert sample.pod_count == 2


def test_cri_runtime_list_workload_metrics_uses_delta_fallback(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="node-a")
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)
    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    calls = [
        SimpleNamespace(
            stats=_stats(
                pod_id="pod-1",
                app_name="default/demo",
                timestamp=1_000_000_000,
                usage_core_nano_seconds=1_000_000_000,
                memory_bytes=100,
            )
        ),
        SimpleNamespace(
            stats=_stats(
                pod_id="pod-1",
                app_name="default/demo",
                timestamp=2_000_000_000,
                usage_core_nano_seconds=3_000_000_000,
                memory_bytes=100,
            )
        ),
    ]

    def _runtime_call(method, req):
        if method == "ListPodSandbox":
            return SimpleNamespace(items=[_pod(pod_id="pod-1", app_name="default/demo")])
        assert method == "PodSandboxStats"
        return calls.pop(0)

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    first = runtime.list_workload_metrics()
    second = runtime.list_workload_metrics()

    assert first[0].cpu_cores is None
    assert second[0].cpu_cores == 2.0


def test_cri_runtime_list_workload_metrics_skips_stale_sandbox_stats(monkeypatch) -> None:
    runtime = CRIRuntime(node_id="node-a")
    monkeypatch.setattr(runtime, "_ensure_clients", lambda: None)
    monkeypatch.setattr(runtime, "_pb2", lambda: _PB2)
    fake_grpc = _install_fake_grpc(monkeypatch)

    class _FakeRpcError(fake_grpc.RpcError):
        def code(self):
            return fake_grpc.StatusCode.UNKNOWN

        def details(self):
            return (
                'failed to decode sandbox container metrics for sandbox "pod-racy": '
                'failed to get pod sandbox stats since sandbox container "pod-racy" '
                "is not in ready state"
            )

    def _runtime_call(method, req):
        if method == "ListPodSandbox":
            return SimpleNamespace(
                items=[
                    _pod(pod_id="pod-good", app_name="default/demo"),
                    _pod(pod_id="pod-racy", app_name="default/demo"),
                ]
            )
        assert method == "PodSandboxStats"
        if req.pod_sandbox_id == "pod-racy":
            raise _FakeRpcError()
        return SimpleNamespace(
            stats=_stats(
                pod_id="pod-good",
                app_name="default/demo",
                timestamp=2_000_000_000,
                usage_nano_cores=500_000_000,
                memory_bytes=128,
            )
        )

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    samples = runtime.list_workload_metrics()

    assert len(samples) == 1
    assert samples[0].app_name == "default/demo"
    assert samples[0].pod_count == 1


def test_stale_pod_sandbox_error_matches_not_ready_state_message() -> None:
    runtime = CRIRuntime(node_id="node-a")
    fake_grpc = SimpleNamespace(
        RpcError=type("_FakeRpcErrorBase", (Exception,), {}),
        StatusCode=SimpleNamespace(
            UNKNOWN="unknown",
            NOT_FOUND="not_found",
            FAILED_PRECONDITION="failed_precondition",
        ),
    )
    original_grpc = cri_runtime_module.grpc
    cri_runtime_module.grpc = fake_grpc

    class _FakeRpcError(fake_grpc.RpcError):
        def code(self):
            return fake_grpc.StatusCode.UNKNOWN

        def details(self):
            return (
                'failed to decode sandbox container metrics for sandbox "pod-racy": '
                'failed to get pod sandbox stats since sandbox container "pod-racy" '
                "is not in ready state"
            )

    try:
        assert runtime._is_stale_pod_sandbox_error(_FakeRpcError()) is True
    finally:
        cri_runtime_module.grpc = original_grpc
