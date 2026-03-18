from __future__ import annotations

from types import SimpleNamespace

from ae.runtime.cri_runtime import CRIRuntime


class _PB2:
    class ListPodSandboxStatsRequest:
        pass


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
    monkeypatch.setattr(
        runtime,
        "_runtime_call",
        lambda method, req: SimpleNamespace(
            stats=[
                _stats(
                    pod_id="pod-1",
                    app_name="default/demo",
                    timestamp=2_000_000_000,
                    usage_nano_cores=500_000_000,
                    memory_bytes=134217728,
                ),
                _stats(
                    pod_id="pod-2",
                    app_name="default/demo",
                    timestamp=2_000_000_000,
                    usage_nano_cores=250_000_000,
                    memory_bytes=67108864,
                ),
            ]
        ),
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
            stats=[
                _stats(
                    pod_id="pod-1",
                    app_name="default/demo",
                    timestamp=1_000_000_000,
                    usage_core_nano_seconds=1_000_000_000,
                    memory_bytes=100,
                )
            ]
        ),
        SimpleNamespace(
            stats=[
                _stats(
                    pod_id="pod-1",
                    app_name="default/demo",
                    timestamp=2_000_000_000,
                    usage_core_nano_seconds=3_000_000_000,
                    memory_bytes=100,
                )
            ]
        ),
    ]

    def _runtime_call(method, req):
        return calls.pop(0)

    monkeypatch.setattr(runtime, "_runtime_call", _runtime_call)

    first = runtime.list_workload_metrics()
    second = runtime.list_workload_metrics()

    assert first[0].cpu_cores is None
    assert second[0].cpu_cores == 2.0
