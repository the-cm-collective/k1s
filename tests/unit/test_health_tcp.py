from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, AppSpec, Metadata, HealthSpec, ProbeSpec
from ae.runtime.base import RuntimeResult, ReplicaState


def test_tcp_probe_success(monkeypatch):
    hm = HealthManager()
    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(readiness=ProbeSpec(tcpSocket={"port": 8080}, timeoutSeconds=1, periodSeconds=0)),  # type: ignore[arg-type]
    )
    m = AppManifest(apiVersion="ae.dev/v1alpha1", kind="App", metadata=Metadata(name="tcp"), spec=spec)
    r = RuntimeResult(revision=1, created=1, updated=0, removed=0, replica_states=[ReplicaState(replica_id="tcp-rev1-0", ready=False, status="running", endpoint="127.0.0.1:51234")])

    class OKSock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_conn(addr, timeout=1):  # noqa: ANN001
        return OKSock()

    import socket as _socket

    monkeypatch.setattr(_socket, "create_connection", fake_conn)

    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 1


def test_tcp_probe_failure(monkeypatch):
    hm = HealthManager()
    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(readiness=ProbeSpec(tcpSocket={"port": 8080}, timeoutSeconds=1, periodSeconds=0)),  # type: ignore[arg-type]
    )
    m = AppManifest(apiVersion="ae.dev/v1alpha1", kind="App", metadata=Metadata(name="tcp"), spec=spec)
    r = RuntimeResult(revision=1, created=1, updated=0, removed=0, replica_states=[ReplicaState(replica_id="tcp-rev1-0", ready=False, status="running", endpoint="127.0.0.1:51234")])

    def fake_conn(addr, timeout=1):  # noqa: ANN001
        raise OSError("connection refused")

    import socket as _socket

    monkeypatch.setattr(_socket, "create_connection", fake_conn)

    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 0

