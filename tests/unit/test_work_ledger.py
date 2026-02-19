from pathlib import Path

from ae.controller.state import SQLiteStateStore


def test_work_ledger_reschedule(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)

    work_id = "work-1"
    site_id = "site-1"
    payload = {"work_id": work_id, "attempt": 1, "site_id": site_id}

    store.upsert_work_ledger(
        work_id=work_id,
        attempt=1,
        site_id=site_id,
        state="Dispatched",
        desired_generation=None,
    )
    store.enqueue_work_outbox(work_id, 1, site_id, payload)

    new_attempt = store.reschedule_work(work_id=work_id, attempt=1)
    assert new_attempt == 2

    ledger = store.get_work_ledger(work_id)
    assert ledger is not None
    assert ledger.attempt == 2
    assert ledger.state == "Pending"

    payload2 = store.get_outbox_payload(work_id, 2)
    assert payload2 is not None
    assert payload2["attempt"] == 2
