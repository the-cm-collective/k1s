from datetime import datetime, timedelta, timezone
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
    assert result.replay_attempts == 0
    assert result.last_replay_at is None
    assert result.next_retry_at is None
    assert result.last_replay_error is None
    assert result.controller_id == "ctrl-a"
    assert result.controller_epoch == 7
    assert result.operation_id == "work:w1:1"

    undelivered = spool.list_undelivered_results()
    assert len(undelivered) == 1
    assert spool.count_undelivered_results() == 1
    spool.mark_result_delivered("w1", 1)
    assert spool.list_undelivered_results() == []


def test_gateway_spool_replay_schedule_and_reset(tmp_path: Path) -> None:
    spool = GatewaySpool(tmp_path / "spool.db")
    spool.init()
    payload = {
        "work_id": "w2",
        "attempt": 1,
        "status": "failed",
        "controller_id": "ctrl-a",
        "controller_epoch": 7,
        "operation_id": "work:w2:1",
    }
    spool.record_result("w2", 1, "failed", payload)
    ready = spool.list_replay_ready_results()
    assert len(ready) == 1
    assert ready[0].work_id == "w2"

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    spool.record_result_delivery_attempt("w2", 1, error="nats down", retry_at=retry_at)
    record = spool.get_result("w2", 1)
    assert record is not None
    assert record.replay_attempts == 1
    assert record.last_replay_at is not None
    assert record.last_replay_error == "nats down"
    assert record.next_retry_at is not None
    assert spool.list_replay_ready_results() == []
    assert spool.list_replay_ready_results(now=retry_at + timedelta(seconds=1))

    spool.reset_replay_schedule()
    reset_record = spool.get_result("w2", 1)
    assert reset_record is not None
    assert reset_record.next_retry_at is None
    assert spool.list_replay_ready_results()
