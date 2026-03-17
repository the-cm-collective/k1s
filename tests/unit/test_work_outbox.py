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


def test_outbox_publish_identity_survives_store_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    first = SQLiteStateStore(db_path=db_path)
    first.enqueue_work_outbox(
        "w2",
        3,
        "site-2",
        {
            "work_id": "w2",
            "attempt": 3,
            "operation_id": "work:w2:3",
        },
    )
    first.record_outbox_publish_attempt("w2", 3, error="leader lost")

    second = SQLiteStateStore(db_path=db_path)
    entries = second.list_outbox_unpublished(limit=10)
    assert len(entries) == 1
    assert entries[0].publish_subject == "k1s.v1.work.site.site-2"
    assert entries[0].publish_msg_id == "work:w2:3"
    assert entries[0].publish_attempts == 1
    assert entries[0].last_publish_error == "leader lost"
