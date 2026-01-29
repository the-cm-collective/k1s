from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, AppSpec, HealthSpec, Metadata, ProbeSpec
from ae.runtime.base import PodState, RuntimeResult


class DummyResp:
    def __init__(self, code: int):
        self.status_code = code


def test_readiness_success_threshold(monkeypatch):
    hm = HealthManager()

    # Manifest with readiness successThreshold=2, failureThreshold=2
    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(
            readiness=ProbeSpec(
                httpGet={"path": "/healthz", "port": 8080},
                successThreshold=2,
                failureThreshold=2,
                periodSeconds=0,
            )
        ),  # type: ignore[arg-type]
    )
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1", kind="App", metadata=Metadata(name="t"), spec=spec
    )

    # Replica with endpoint
    r = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="t-rev1-0", ready=False, status="running", endpoint="127.0.0.1:8080"
            )
        ],
    )

    # Make requests.get return 200 twice
    calls = {"n": 0}

    def fake_get(_url, timeout):  # noqa: ANN001
        _ = timeout
        calls["n"] += 1
        return DummyResp(200)

    import ae.controller.health as mod

    monkeypatch.setattr(mod, "get", fake_get)

    rep = hm.evaluate(m, r)
    # first success: not yet ready due to successThreshold=2
    assert rep.ready_replicas == 0

    rep = hm.evaluate(m, r)
    # second consecutive success: becomes ready
    assert rep.ready_replicas == 1


def test_readiness_failure_threshold(monkeypatch):
    hm = HealthManager()
    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(
            readiness=ProbeSpec(
                httpGet={"path": "/healthz", "port": 8080},
                successThreshold=1,
                failureThreshold=2,
                periodSeconds=0,
            )
        ),  # type: ignore[arg-type]
    )
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1", kind="App", metadata=Metadata(name="t"), spec=spec
    )
    r = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        pod_states=[
            PodState(
                pod_name="t-rev1-0", ready=False, status="running", endpoint="127.0.0.1:8080"
            )
        ],
    )

    # Simulate 500 then 500; readiness should only flip to false after two consecutive failures
    codes = iter([200, 500, 500])

    def fake_get(_url, timeout):  # noqa: ANN001
        _ = timeout
        return DummyResp(next(codes))

    import ae.controller.health as mod

    monkeypatch.setattr(mod, "get", fake_get)

    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 1  # initial 200
    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 1  # first fail below threshold retains ready
    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 0  # second consecutive fail crosses threshold
