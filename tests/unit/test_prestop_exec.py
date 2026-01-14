from pathlib import Path

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore
from ae.runtime.base import ReplicaState, RuntimeResult


class DummyRuntimeWithExec:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, list[str], int | None]] = []
        self.removed = 0

    def ensure_app(self, manifest, revision, *, _keep_old=False, _limit_create=None):  # noqa: ANN001
        # Create one new replica state so readiness gating passes
        return RuntimeResult(
            revision=revision,
            created=1,
            updated=0,
            removed=0,
            replica_states=[
                ReplicaState(
                    replica_id=f"{manifest.metadata.name}-rev{revision}-0",
                    ready=True,
                    status="running",
                    endpoint="127.0.0.1:9000",
                )
            ],
        )

    def list_containers_info(self):  # noqa: D401 - test stub
        # One old replica and one current; only old should be targeted
        return [
            {
                "name": "ae-echo-rev0-0",
                "labels": {
                    "ae.app": "echo",
                    "ae.revision": "0",
                    "ae.replica_id": "echo-rev0-0",
                },
                "host_ports": [18080],
            },
            {
                "name": "ae-echo-rev1-0",
                "labels": {
                    "ae.app": "echo",
                    "ae.revision": "1",
                    "ae.replica_id": "echo-rev1-0",
                },
                "host_ports": [18080],
            },
        ]

    def exec(self, replica_id, command, timeout=None):  # noqa: ANN001
        self.exec_calls.append((replica_id, list(command), timeout))
        return 0

    def remove_old_revisions(self, _app_name: str, _keep_revision: int) -> int:
        self.removed += 1
        return 1


def test_prestop_exec_runs_before_removal(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntimeWithExec()
    rec = Reconciler(rt, store)
    man = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="echo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            terminationGracePeriodSeconds=5,
            lifecycle={"preStop": {"exec": {"command": ["/bin/sh", "-c", "echo bye"]}}},  # type: ignore[arg-type]
        ),
    )
    rec.reconcile(man)
    # Should have attempted exec on old replica id only
    assert any(call[0] == "echo-rev0-0" for call in rt.exec_calls)
    assert all(isinstance(call[2], int) and call[2] == 5 for call in rt.exec_calls)
    assert rt.removed > 0


def test_prestop_http_and_tcp_emit_events(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    rt = DummyRuntimeWithExec()
    rec = Reconciler(rt, store)
    # Monkeypatch requests.get and socket.create_connection to avoid real I/O
    calls = {"http": 0, "tcp": 0}

    class _DummySock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_get(_url: str, *args, **kwargs):  # noqa: ANN001
        _ = (args, kwargs)
        calls["http"] += 1
        class _R:
            status_code = 200
        return _R()

    def fake_conn(_addr, *args, **kwargs):  # noqa: ANN001
        _ = (args, kwargs)
        calls["tcp"] += 1
        return _DummySock()

    monkeypatch.setattr("requests.get", fake_get, raising=False)
    import socket as _sock
    monkeypatch.setattr(_sock, "create_connection", fake_conn, raising=False)

    # HTTP preStop
    man_http = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="echo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            lifecycle={"preStop": {"httpGet": {"path": "/quit", "port": 8080}}},  # type: ignore[arg-type]
        ),
    )
    rec.reconcile(man_http)

    # TCP preStop
    man_tcp = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="echo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            lifecycle={"preStop": {"tcpSocket": {"port": 8080}}},  # type: ignore[arg-type]
        ),
    )
    rec.reconcile(man_tcp)

    assert calls["http"] >= 1
    assert calls["tcp"] >= 1
