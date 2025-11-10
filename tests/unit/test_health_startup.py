from datetime import datetime, timedelta, timezone

from ae.controller.health import HealthManager
from ae.controller.spec import (
    AppManifest,
    AppSpec,
    HealthSpec,
    Metadata,
    ProbeSpec,
)
from ae.runtime.base import ReplicaState, RuntimeResult
from requests import Response


def test_startup_gates_liveness_and_readiness(monkeypatch):
    # startup initialDelay not elapsed -> readiness false, liveness true, no HTTP called
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                startup=ProbeSpec(httpGet={"path": "/start", "port": 8080}, initialDelaySeconds=30),  # type: ignore[arg-type]
                readiness=ProbeSpec(httpGet={"path": "/healthz", "port": 8080}),  # type: ignore[arg-type]
                liveness=ProbeSpec(httpGet={"path": "/live", "port": 8080}),  # type: ignore[arg-type]
            ),
        ),
    )
    start_time = datetime.now(timezone.utc) - timedelta(seconds=5)
    result = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        replica_states=[
            ReplicaState(replica_id="demo-0", ready=False, endpoint="127.0.0.1:8080", started_at=start_time),
        ],
    )

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        raise AssertionError("HTTP probes should be gated by startup and not invoked yet")

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)
    assert report.ready_replicas == 0
    assert report.live_replicas == 1
    assert report.replicas[0].ready is False
    assert report.replicas[0].live is True
    assert "startup" in report.replicas[0].readiness_message.lower()


def test_startup_pass_enables_probes(monkeypatch):
    # startup exec succeeds immediately -> readiness/liveness are evaluated
    hm = HealthManager()
    hm.set_exec_callback(lambda rid, cmd, t: 0)

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                startup=ProbeSpec(exec={"command": ["/bin/true"]}, periodSeconds=0),  # type: ignore[arg-type]
                readiness=ProbeSpec(httpGet={"path": "/healthz", "port": 8080}, periodSeconds=0),  # type: ignore[arg-type]
                liveness=ProbeSpec(httpGet={"path": "/live", "port": 8080}, periodSeconds=0),  # type: ignore[arg-type]
            ),
        ),
    )
    result = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        replica_states=[
            ReplicaState(replica_id="demo-0", ready=False, endpoint="127.0.0.1:8080"),
        ],
    )

    ok = Response(); ok.status_code = 200

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        return ok

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = hm.evaluate(manifest, result)
    assert report.live_replicas == 1
    assert report.ready_replicas == 1
    r = report.replicas[0]
    assert r.ready is True and r.live is True
    assert "http 200" in r.readiness_message
