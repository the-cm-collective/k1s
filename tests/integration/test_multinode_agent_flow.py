"""Integration-ish test that exercises multi-node reconcile against stub agents.

The controller scheduler should distribute replicas across Ready nodes, route
runtime calls to each node's agent endpoint, and persist replica state without
touching the local runtime.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ae.controller.health import HealthManager
from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.runtime.base import ReplicaState, RuntimeAdapter, RuntimeResult


class DummyLocalRuntime(RuntimeAdapter):
    """Local runtime stub used to prove that remote agent paths are exercised."""

    def __init__(self) -> None:
        self.ensure_calls = 0
        self.remove_old_calls = 0

    def ensure_app(
        self,
        manifest: AppManifest,
        revision: int,
        *,
        _keep_old: bool = False,
        _limit_create: int | None = None,
        replica_ids: list[str] | None = None,
        _node_id: str | None = None,
    ) -> RuntimeResult:
        self.ensure_calls += 1
        rids = replica_ids or [
            f"{manifest.metadata.name}-rev{revision}-{i}" for i in range(manifest.spec.replicas)
        ]
        return RuntimeResult(
            revision=revision,
            created=len(rids),
            updated=0,
            removed=0,
            replica_states=[
                ReplicaState(replica_id=rid, ready=True, status="running", endpoint="127.0.0.1:0")
                for rid in rids
            ],
        )

    def remove_app(self, _app_name: str) -> int:  # pragma: no cover - not used here
        return 0

    def remove_old_revisions(self, _app_name: str, _keep_revision: int) -> int:
        self.remove_old_calls += 1
        return 0

    def read_logs(
        self,
        _replica_id: str,
        *,
        _follow: bool = False,
        _tail: int | None = None,
        _since: int | None = None,
    ) -> Iterable[str]:  # pragma: no cover - not used here
        return iter([])

    def ensure_storage_volumes(
        self, _app_name: str, _volumes: list[dict]
    ) -> None:  # pragma: no cover
        return None

    def remove_storage_volumes(self, _app_name: str, _names: list[str]) -> int:  # pragma: no cover
        return 0

    def list_storage_volumes(self, _app_name: str | None = None) -> list[dict]:  # pragma: no cover
        return []

    def list_containers_info(self) -> list[dict]:  # pragma: no cover
        return []

    def exec(self, _replica_id: str, _command: list[str], *, _timeout: int | None = None) -> int:
        return 0


def _start_agent(node_id: str):
    """Spin up a stub HTTP agent that echoes ensure/remove_old calls."""

    ensure_calls: list[dict] = []
    remove_calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/v1/ensure_app":
                ensure_calls.append(payload)
                rids = payload.get("replica_ids") or []
                resp = {
                    "revision": int(payload.get("revision", 0)),
                    "created": len(rids),
                    "updated": 0,
                    "removed": 0,
                    "replica_states": [
                        {
                            "replica_id": rid,
                            "ready": True,
                            "status": "running",
                            "endpoint": f"{node_id}:10000",
                        }
                        for rid in rids
                    ],
                }
                return self._json(resp)

            if self.path == "/v1/remove_old":
                remove_calls.append(payload)
                return self._json({"removed": 0})

            return self._json({"error": "not found"}, status=404)

        def log_message(self, _fmt, *_args):  # noqa: ANN001
            return  # Silence noisy stdout during tests

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, ensure_calls, remove_calls


def test_reconcile_targets_per_node_agents(tmp_path):
    agent1, t1, ensure1, _ = _start_agent("n1")
    agent2, t2, ensure2, _ = _start_agent("n2")
    try:
        store = SQLiteStateStore(tmp_path / "state.db")
        store.upsert_node(
            "n1",
            name="node1",
            labels={},
            taints=[],
            backend="podman",
            endpoint=f"http://127.0.0.1:{agent1.server_port}",
        )
        store.upsert_node(
            "n2",
            name="node2",
            labels={},
            taints=[],
            backend="podman",
            endpoint=f"http://127.0.0.1:{agent2.server_port}",
        )
        store.record_heartbeat("n1", "Ready")
        store.record_heartbeat("n2", "Ready")

        runtime = DummyLocalRuntime()
        reconciler = Reconciler(runtime=runtime, state_store=store, health_manager=HealthManager())
        manifest = AppManifest(
            apiVersion="ae.dev/v1alpha1",
            kind="App",
            metadata=Metadata(name="echo-mn"),
            spec=AppSpec(image="busybox", replicas=2),
        )

        report = reconciler.reconcile(manifest)

        assert report.ready_replicas == 2
        # Controller should call each agent once with its assigned replica ids
        assert runtime.ensure_calls == 0
        assert len(ensure1) == 1
        assert len(ensure2) == 1
        assert ensure1[0].get("node_id") == "n1"
        assert ensure2[0].get("node_id") == "n2"
        assigned = (ensure1[0].get("replica_ids") or []) + (ensure2[0].get("replica_ids") or [])
        # unique replica ids across nodes
        assert len(set(assigned)) == 2
        assert len(assigned) == 2

        replicas = store.list_replicas("echo-mn")
        assert len(replicas) == 2
        assert all(r.ready for r in replicas)
    finally:
        agent1.shutdown()
        agent1.server_close()
        agent2.shutdown()
        agent2.server_close()
        t1.join(timeout=2)
        t2.join(timeout=2)
