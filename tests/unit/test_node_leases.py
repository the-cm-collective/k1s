from datetime import datetime, timedelta, timezone
from pathlib import Path

from ae.controller.state import SQLiteStateStore


def test_node_lease_acquire_and_renew(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    lease = store.acquire_lease(
        site_id="site-1",
        node_id="node-1",
        session_id="sess-1",
        lease_ttl_ms=60000,
        renew_after_ms=20000,
        controller_epoch=1,
    )
    assert lease.lease_id
    renewed, reason = store.renew_lease(
        node_id="node-1", session_id="sess-1", lease_id=lease.lease_id
    )
    assert reason is None
    assert renewed is not None


def test_node_lease_invalid_session(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    lease = store.acquire_lease(
        site_id="site-1",
        node_id="node-1",
        session_id="sess-1",
        lease_ttl_ms=60000,
        renew_after_ms=20000,
        controller_epoch=1,
    )
    renewed, reason = store.renew_lease(
        node_id="node-1", session_id="sess-2", lease_id=lease.lease_id
    )
    assert renewed is None
    assert reason == "invalid_session"


def test_node_lease_expired(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    lease = store.acquire_lease(
        site_id="site-1",
        node_id="node-1",
        session_id="sess-1",
        lease_ttl_ms=1000,
        renew_after_ms=200,
        controller_epoch=1,
    )
    expired_at = lease.expires_at + timedelta(seconds=1)
    renewed, reason = store.renew_lease(
        node_id="node-1",
        session_id="sess-1",
        lease_id=lease.lease_id,
        now=expired_at,
    )
    assert renewed is None
    assert reason == "expired"
