from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from ae.controller.inference_cell import FabricSessionInfo, HttpFabricAgentClient
from ae.controller.spec import AppManifest
from ae.runtime import StubRuntime
from ae.runtime.remote_runtime import RemoteRuntime


class _Authority:
    def __init__(self, controller_id: str = "ctrl-a", epoch: int = 11) -> None:
        self._controller_id = controller_id
        self._epoch = epoch

    def snapshot(self):
        return SimpleNamespace(
            is_leader=True,
            leader_info=SimpleNamespace(
                controller_id=self._controller_id,
                controller_epoch=self._epoch,
            ),
        )


def _manifest() -> AppManifest:
    return AppManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "Deployment",
            "metadata": {"name": "demo"},
            "spec": {
                "image": "alpine:3.20",
                "replicas": 1,
            },
        }
    )


def test_remote_runtime_includes_fencing_envelope_on_ensure() -> None:
    captured: dict[str, object] = {}
    runtime = RemoteRuntime(
        "http://agent:9112",
        StubRuntime(),
        authority=_Authority("ctrl-a", 11),
        node_id="node-a",
    )

    def _fake_request(method: str, path: str, *, json=None, timeout: int = 30, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        return SimpleNamespace(
            json=lambda: {
                "revision": 3,
                "created": 1,
                "updated": 0,
                "removed": 0,
                "pod_states": [],
            }
        )

    runtime._request = _fake_request  # type: ignore[method-assign]
    runtime.ensure_app(_manifest(), 3, node_id="node-a")

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["controller_id"] == "ctrl-a"
    assert payload["controller_epoch"] == 11
    assert payload["operation_id"] == "ensure:demo:3:node-a"


def test_fabric_agent_client_includes_fencing_envelope(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Store:
        def get_node(self, node_id: str):
            return SimpleNamespace(endpoint="http://agent:9112"), None

    def _fake_post(url, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return SimpleNamespace(status_code=200, content=b'{"ok": true}', json=lambda: {"ok": True})

    monkeypatch.setattr("ae.controller.inference_cell.requests.post", _fake_post)
    client = HttpFabricAgentClient(_Store(), authority=_Authority("ctrl-b", 14))
    session = FabricSessionInfo(
        session_id="s-1234",
        ifname="wg-cell-test",
        member_ips={"node-a": "10.250.1.1"},
        expires_at=datetime.now(timezone.utc),
        policy_mode="strict_membership",
        allowed_rules=[{"proto": "tcp", "port": 18080}],
    )

    assert client.ensure_session("node-a", session) is True
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["controller_id"] == "ctrl-b"
    assert payload["controller_epoch"] == 14
    assert payload["operation_id"] == "fabric.ensure:s-1234:node-a"


def test_remote_runtime_proxies_workload_metrics() -> None:
    runtime = RemoteRuntime(
        "http://agent:9112",
        StubRuntime(),
        authority=_Authority("ctrl-a", 11),
        node_id="node-a",
    )

    def _fake_request(method: str, path: str, *, timeout: int = 30, **kwargs):
        assert method == "GET"
        assert path == "/v1/workload_metrics"
        return SimpleNamespace(
            json=lambda: {
                "items": [
                    {
                        "app_name": "default/demo",
                        "node_id": "node-a",
                        "collected_at": "2026-03-18T12:00:00+00:00",
                        "cpu_cores": 1.5,
                        "memory_bytes": 268435456,
                        "pod_count": 3,
                    }
                ]
            }
        )

    runtime._request = _fake_request  # type: ignore[method-assign]

    samples = runtime.list_workload_metrics()

    assert len(samples) == 1
    assert samples[0].app_name == "default/demo"
    assert samples[0].node_id == "node-a"
    assert samples[0].cpu_cores == 1.5
    assert samples[0].memory_bytes == 268435456
    assert samples[0].pod_count == 3


def test_remote_runtime_port_forward_uses_upgrade_endpoint() -> None:
    runtime = RemoteRuntime(
        "http://agent:9112",
        StubRuntime(),
        authority=_Authority("ctrl-a", 11),
        node_id="node-a",
    )
    captured: dict[str, object] = {}
    sentinel = object()

    def _fake_open_upgrade(path: str, payload: dict, upgrade: str):
        captured["path"] = path
        captured["payload"] = payload
        captured["upgrade"] = upgrade
        return sentinel, {}

    runtime._open_upgrade = _fake_open_upgrade  # type: ignore[method-assign]

    sock = runtime.port_forward_socket(
        pod_id=None,
        pod_name="demo-rev1-0",
        namespace="default",
        port=8080,
    )

    assert sock is sentinel
    assert captured["path"] == "/v1/portforward/attach"
    assert captured["upgrade"] == "ae-portforward"
    assert captured["payload"] == {
        "pod_id": None,
        "pod_name": "demo-rev1-0",
        "namespace": "default",
        "port": 8080,
    }


def test_remote_runtime_port_forward_delegates_to_local_runtime() -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    class _LocalRuntime(StubRuntime):
        def port_forward_socket(
            self,
            *,
            pod_id: str | None,
            pod_name: str | None,
            namespace: str | None,
            port: int,
        ):
            captured["pod_id"] = pod_id
            captured["pod_name"] = pod_name
            captured["namespace"] = namespace
            captured["port"] = port
            return sentinel

    runtime = RemoteRuntime(
        None,
        _LocalRuntime(),
        authority=_Authority("ctrl-a", 11),
        node_id="node-a",
    )

    sock = runtime.port_forward_socket(
        pod_id="pod-123",
        pod_name="demo-rev1-0",
        namespace="default",
        port=8080,
    )

    assert sock is sentinel
    assert captured == {
        "pod_id": "pod-123",
        "pod_name": "demo-rev1-0",
        "namespace": "default",
        "port": 8080,
    }
