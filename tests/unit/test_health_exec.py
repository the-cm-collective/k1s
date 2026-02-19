from ae.controller.health import HealthManager
from ae.controller.spec import AppManifest, AppSpec, HealthSpec, Metadata, ProbeSpec
from ae.runtime.base import PodState, RuntimeResult


def test_exec_probe_success():
    hm = HealthManager()
    # Inject a fake exec that returns 0
    hm.set_exec_callback(lambda _rid, _cmd, _timeout: 0)

    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(
            readiness=ProbeSpec(
                exec={"command": ["sh", "-c", "true"]}, timeoutSeconds=1, periodSeconds=0
            )
        ),  # type: ignore[arg-type]
    )
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1", kind="Deployment", metadata=Metadata(name="e1"), spec=spec
    )
    r = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        pod_states=[
            PodState(pod_name="e1-rev1-0", ready=False, status="running", endpoint=None)
        ],
    )
    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 1


def test_exec_probe_failure():
    hm = HealthManager()
    hm.set_exec_callback(lambda _rid, _cmd, _timeout: 1)

    spec = AppSpec(
        image="alpine:3.20",
        health=HealthSpec(
            readiness=ProbeSpec(
                exec={"command": ["sh", "-c", "exit 1"]}, timeoutSeconds=1, periodSeconds=0
            )
        ),  # type: ignore[arg-type]
    )
    m = AppManifest(
        apiVersion="ae.dev/v1alpha1", kind="Deployment", metadata=Metadata(name="e2"), spec=spec
    )
    r = RuntimeResult(
        revision=1,
        created=1,
        updated=0,
        removed=0,
        pod_states=[
            PodState(pod_name="e2-rev1-0", ready=False, status="running", endpoint=None)
        ],
    )
    rep = hm.evaluate(m, r)
    assert rep.ready_replicas == 0
