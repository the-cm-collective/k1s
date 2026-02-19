"""Tests for the health manager."""

from datetime import UTC

from requests import Response

from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, AppSpec, HealthSpec, Metadata, ProbeSpec
from ae.runtime.base import PodState, RuntimeResult


def test_health_manager_counts_ready():
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name="demo"),
        spec=AppSpec(image="alpine:3.20", replicas=2),
    )

    result = RuntimeResult(
        revision=1,
        created=2,
        updated=0,
        removed=0,
        pod_states=[
            PodState(pod_name="demo-0", ready=True),
            PodState(pod_name="demo-1", ready=False),
        ],
    )

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1
    assert report.live_replicas == 2
    assert len(report.pods) == 2
    assert any(replica.pod_name == "demo-1" and not replica.ready for replica in report.pods)
    assert all(replica.live for replica in report.pods)


def test_health_manager_http_probe(monkeypatch):
    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
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
        pod_states=[
            PodState(pod_name="demo-0", ready=False, endpoint="127.0.0.1:8080"),
        ],
    )

    response = Response()
    response.status_code = 200

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        _ = timeout
        assert url == "http://127.0.0.1:8080/healthz"
        return response

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1
    assert report.pods[0].ready is True
    assert "readiness http 200" in report.pods[0].readiness_message


def test_health_manager_loopback_fallback(monkeypatch):
    monkeypatch.setenv("AE_PROBE_LOOPBACK_FALLBACK", "host.docker.internal")

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
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
        pod_states=[
            PodState(pod_name="demo-0", ready=False, endpoint="127.0.0.1:8080"),
        ],
    )

    response = Response()
    response.status_code = 200

    def fake_get(url: str, timeout: int):  # noqa: ANN001
        _ = timeout
        assert url == "http://host.docker.internal:8080/healthz"
        return response

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 1


def test_health_manager_initial_delay(monkeypatch):
    from datetime import datetime, timedelta

    manifest = AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
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

    start_time = datetime.now(UTC) - timedelta(seconds=5)
    result = RuntimeResult(
        revision=2,
        created=1,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="demo-0",
                ready=True,
                endpoint="127.0.0.1:8080",
                started_at=start_time,
            ),
        ],
    )

    def fake_get(_url: str, timeout: int):  # noqa: ANN001
        _ = timeout
        raise AssertionError("HTTP probe should not be called during initial delay")

    monkeypatch.setattr("ae.controller.health.get", fake_get)

    report = HealthManager().evaluate(manifest, result)

    assert report.ready_replicas == 0
    assert report.pods[0].readiness_message.startswith("waiting initial delay")
