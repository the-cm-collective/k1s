from __future__ import annotations

import threading
from http.server import HTTPServer

import pytest

from ae.controller.spec import AppManifest
from ae.node.server import AgentHandler
from ae.runtime import RuntimeAdapter, RuntimeResult
from ae.runtime.remote_runtime import RemoteRuntime
from ae.storage.netfs import PvcNotReadyError
from ae.storage.types import PvcRef


class DummyRuntime(RuntimeAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def ensure_app(  # type: ignore[override]
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        self.calls += 1
        return RuntimeResult(revision=revision, created=0, updated=0, removed=0, pod_states=[])

    def read_logs(
        self,
        pod_name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        return iter([])

    def remove_app(self, app_name: str) -> int:
        return 0

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        return 0

    def ensure_storage_volumes(self, app_name: str, volumes: list[dict]) -> None:
        return None

    def remove_storage_volumes(self, app_name: str, names: list[str]) -> int:
        return 0


class DummyLocalRuntime(DummyRuntime):
    pass


class FailingVolumeManager:
    def inject_pvc_mounts(self, *_a, **_k):  # noqa: ANN001
        pvc = PvcRef(namespace="default", name="pvc-demo")
        raise PvcNotReadyError(pvc, "PVC not ready")


def _start_agent(runtime: RuntimeAdapter):
    AgentHandler.runtime = runtime  # type: ignore[assignment]
    AgentHandler.node_id = "node-test"
    AgentHandler.volume_manager = FailingVolumeManager()
    try:
        server = HTTPServer(("127.0.0.1", 0), AgentHandler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_agent_returns_pending_when_pvc_not_ready():
    runtime = DummyRuntime()
    agent, thread = _start_agent(runtime)
    try:
        remote = RemoteRuntime(
            f"http://127.0.0.1:{agent.server_port}",
            DummyLocalRuntime(),
        )
        manifest = AppManifest.model_validate(
            {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {"image": "alpine:3.20", "replicas": 1},
            }
        )
        result = remote.ensure_app(
            manifest,
            revision=1,
            pod_names=["demo-rev1-0"],
            node_id="node-test",
        )
        assert runtime.calls == 0
        assert result.pod_states
        assert result.pod_states[0].pod_name == "demo-rev1-0"
        assert result.pod_states[0].ready is False
        assert result.pod_states[0].status == "Pending"
    finally:
        AgentHandler.volume_manager = None
        agent.shutdown()
        agent.server_close()
        thread.join(timeout=2)
