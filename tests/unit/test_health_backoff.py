import time

from ae.controller.health import HealthManager
from ae.controller.spec import ProbeSpec
from ae.runtime.base import PodState, RuntimeResult


def make_rep(pod_name: str, endpoint: str = "127.0.0.1:1"):
    return PodState(
        pod_name=pod_name, ready=False, status="running", endpoint=endpoint, started_at=None
    )


def test_backoff_after_failures():
    hm = HealthManager()
    # Probe: small period and failureThreshold=1 to trigger backoff quickly
    p = ProbeSpec(tcpSocket={"port": 1}, periodSeconds=1, failureThreshold=1)  # type: ignore[arg-type]
    rep = make_rep("r1")
    res = RuntimeResult(revision=1, created=0, updated=0, removed=0, pod_states=[rep])
    # First evaluation attempts and fails
    hm.evaluate(
        type(
            "M",
            (),
            {
                "spec": type(
                    "S", (), {"health": type("H", (), {"readiness": p, "liveness": None})()}
                )()
            },
        )(),
        res,
    )
    # Next evaluation within period/backoff should include one of the guard messages
    time.sleep(0.05)
    hr = hm.evaluate(
        type(
            "M",
            (),
            {
                "spec": type(
                    "S", (), {"health": type("H", (), {"readiness": p, "liveness": None})()}
                )()
            },
        )(),
        res,
    )
    msg = hr.pods[0].readiness_message.lower()
    assert ("backoff" in msg) or ("waiting period" in msg) or ("transient fail" in msg)
