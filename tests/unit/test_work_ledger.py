from pathlib import Path

from ae.controller.state import SQLiteStateStore
from ae.controller.work_watchdog import WorkWatchdog


def test_work_ledger_reschedule(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)

    work_id = "work-1"
    site_id = "site-1"
    payload = {
        "work_id": work_id,
        "attempt": 1,
        "site_id": site_id,
        "controller_id": "ctrl-a",
        "controller_epoch": 7,
        "operation_id": "work:work-1:1",
    }

    store.upsert_work_ledger(
        work_id=work_id,
        attempt=1,
        site_id=site_id,
        state="Dispatched",
        controller_id="ctrl-a",
        controller_epoch=7,
        operation_id="work:work-1:1",
        desired_generation=None,
    )
    store.enqueue_work_outbox(work_id, 1, site_id, payload)

    new_attempt = store.reschedule_work(
        work_id=work_id,
        attempt=1,
        controller_id="ctrl-b",
        controller_epoch=8,
    )
    assert new_attempt == 2

    ledger = store.get_work_ledger(work_id)
    assert ledger is not None
    assert ledger.attempt == 2
    assert ledger.state == "Pending"
    assert ledger.controller_id == "ctrl-b"
    assert ledger.controller_epoch == 8
    assert ledger.operation_id == "work:work-1:2"

    payload2 = store.get_outbox_payload(work_id, 2)
    assert payload2 is not None
    assert payload2["attempt"] == 2
    assert payload2["controller_id"] == "ctrl-b"
    assert payload2["controller_epoch"] == 8
    assert payload2["operation_id"] == "work:work-1:2"


def test_work_watchdog_reschedule_uses_authority_epoch(tmp_path: Path) -> None:
    db_path = tmp_path / "controller.db"
    store = SQLiteStateStore(db_path=db_path)
    payload = {
        "work_id": "work-2",
        "attempt": 1,
        "site_id": "site-1",
        "controller_id": "ctrl-a",
        "controller_epoch": 3,
        "operation_id": "work:work-2:1",
    }
    store.upsert_work_ledger(
        work_id="work-2",
        attempt=1,
        site_id="site-1",
        state="Dispatched",
        controller_id="ctrl-a",
        controller_epoch=3,
        operation_id="work:work-2:1",
    )
    store.enqueue_work_outbox("work-2", 1, "site-1", payload)

    class _Authority:
        def snapshot(self):
            class _Leader:
                controller_id = "ctrl-c"
                controller_epoch = 9

            class _Snapshot:
                is_leader = True
                leader_info = _Leader()

            return _Snapshot()

    watchdog = WorkWatchdog(store, authority=_Authority())
    watchdog._reschedule("work-2", 1, "test")  # type: ignore[attr-defined]

    ledger = store.get_work_ledger("work-2")
    assert ledger is not None
    assert ledger.attempt == 2
    assert ledger.controller_id == "ctrl-c"
    assert ledger.controller_epoch == 9
    assert ledger.operation_id == "work:work-2:2"
