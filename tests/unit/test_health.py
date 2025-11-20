"""Tests for the health manager."""

from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, AppSpec, Metadata, HealthSpec, ProbeSpec
from ae.runtime.base import ReplicaState, RuntimeResult
from requests import Response


def test_health_manager_counts_ready():
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(image="alpine:3.20", replicas=2),
    )

    result = RuntimeResult(
        revision=1,
        created=2,
        updated=0,
        removed=0,
        replica_states=[
            ReplicaState(replica_id="demo-0", ready=True),
            ReplicaState(replica_id="demo-1", ready=False),
        ],
    )

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1
    assert report.live_replicas == 2
    assert len(report.replicas) == 2
    assert any(replica.replica_id == "demo-1" and not replica.ready for replica in report.replicas)
    assert all(replica.live for replica in report.replicas)


def test_health_manager_http_probe(monkeypatch):
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                readiness=ProbeSpec.model_validate(
                    {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "timeoutSeconds": 1,
                    }
                )
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

    response = Response()
    response.status_code = 200

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        assert url == "http://127.0.0.1:8080/healthz"
        assert timeout == 1
        return response

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1
    assert report.replicas[0].ready is True
    assert "readiness http 200" in report.replicas[0].readiness_message


def test_health_manager_loopback_fallback(monkeypatch):
    monkeypatch.setenv("AE_PROBE_LOOPBACK_FALLBACK", "host.docker.internal")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                readiness=ProbeSpec.model_validate(
                    {
                        "httpGet": {"path": "/healthz", "port": 8080},
                        "timeoutSeconds": 1,
                    }
                )
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

    response = Response()
    response.status_code = 200

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        assert url == "http://host.docker.internal:8080/healthz"
        return response

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1


def test_health_manager_initial_delay(monkeypatch):
    from datetime import datetime, timedelta, timezone

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="App",
        metadata=Metadata(name="demo"),
        spec=AppSpec(
            image="alpine:3.20",
            replicas=1,
            health=HealthSpec(
                readiness=ProbeSpec.model_validate(
                    {
                        "httpGet": {"path": "/live", "port": 8080},
                        "initialDelaySeconds": 30,
                        "timeoutSeconds": 1,
                    }
                )
            ),
        ),
    )

    start_time = datetime.now(timezone.utc) - timedelta(seconds=5)
    result = RuntimeResult(
        revision=2,
        created=1,
        updated=0,
        removed=0,
        replica_states=[
            ReplicaState(
                replica_id="demo-0",
                ready=True,
                endpoint="127.0.0.1:8080",
                started_at=start_time,
            ),
        ],
    )

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        raise AssertionError("HTTP probe should not be called during initial delay")

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 0
    assert report.replicas[0].readiness_message.startswith("waiting initial delay")
