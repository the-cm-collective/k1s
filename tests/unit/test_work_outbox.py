from pathlib import Path

from ae.controller.state import SQLiteStateStore


def test_outbox_enqueue_and_publish_mark(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    store.enqueue_work_outbox(
        "w1",
        1,
        "site-1",
        {
            "work_id": "w1",
            "attempt": 1,
            "operation_id": "work:w1:1",
        },
    )
    entries = store.list_outbox_unpublished(limit=10)
    assert len(entries) == 1
    assert entries[0].work_id == "w1"
    assert entries[0].publish_subject == "k1s.v1.work.site.site-1"
    assert entries[0].publish_msg_id == "work:w1:1"

    store.record_outbox_publish_attempt("w1", 1, error="timeout")
    entries = store.list_outbox_unpublished(limit=10)
    assert entries[0].publish_attempts == 1
    assert entries[0].last_publish_error == "timeout"
    store.mark_outbox_published("w1", 1)
    entries = store.list_outbox_unpublished(limit=10)
    assert entries == []
