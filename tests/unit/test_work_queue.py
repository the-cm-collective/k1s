from pathlib import Path

from ae.controller.state import SQLiteStateStore


def test_work_queue_pull_ack_done(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)

    store.enqueue_work("w1", 1, "site-1", {"work_id": "w1", "attempt": 1})
    store.enqueue_work("w2", 1, "site-1", {"work_id": "w2", "attempt": 1})

    leases = store.pull_work("site-1", limit=1, visibility_timeout_ms=60000)
    assert len(leases) == 1
    assert leases[0].payload["work_id"] == "w1"

    more = store.pull_work("site-1", limit=10, visibility_timeout_ms=60000)
    assert len(more) == 1
    assert more[0].payload["work_id"] == "w2"

    none_left = store.pull_work("site-1", limit=10, visibility_timeout_ms=60000)
    assert none_left == []

    acked = store.ack_work([leases[0].lease_id])
    assert acked == 1

    store.mark_work_done("w1", 1)
    still_none = store.pull_work("site-1", limit=10, visibility_timeout_ms=60000)
    assert still_none == []
