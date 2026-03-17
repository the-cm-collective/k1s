from pathlib import Path

from ae.gateway.spool import GatewaySpool


def test_gateway_spool_roundtrip(tmp_path: Path) -> None:
    spool_path = tmp_path / "spool.db"
    spool = GatewaySpool(spool_path)
    spool.init()

    spool.record_inflight(
        work_id="w1",
        attempt=1,
        js_stream="K1S_WORK",
        js_consumer="WORK_SITE_test",
        js_seq=10,
        node_id="node-1",
        state="accepted",
        payload={
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "work:w1:1",
        },
    )
    assert spool.get_inflight_state("w1", 1) == "accepted"
    inflight = spool.get_inflight_record("w1", 1)
    assert inflight is not None
    assert inflight.controller_id == "ctrl-a"
    assert inflight.controller_epoch == 7
    assert inflight.operation_id == "work:w1:1"

    payload = {
        "work_id": "w1",
        "attempt": 1,
        "status": "succeeded",
        "controller_id": "ctrl-a",
        "controller_epoch": 7,
        "operation_id": "work:w1:1",
    }
    spool.record_result("w1", 1, "succeeded", payload)
    result = spool.get_result("w1", 1)
    assert result is not None
    assert result.status == "succeeded"
    assert result.payload["work_id"] == "w1"
    assert result.controller_id == "ctrl-a"
    assert result.controller_epoch == 7
    assert result.operation_id == "work:w1:1"

    undelivered = spool.list_undelivered_results()
    assert len(undelivered) == 1
    spool.mark_result_delivered("w1", 1)
    assert spool.list_undelivered_results() == []
