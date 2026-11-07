from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ae.controller.node_identity import NodeIdentityConflictError
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


def test_same_session_acquire_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire_lease("site-1", "node-1", "sess-1", 60000, 20000, 1, now=now)
    retry = store.acquire_lease(
        "site-1", "node-1", "sess-1", 60000, 20000, 1, now=now + timedelta(seconds=1)
    )

    assert retry.lease_id == first.lease_id
    assert retry.generation == 1
    assert retry.expires_at == first.expires_at


def test_active_different_session_is_rejected_without_mutation(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire_lease("site-1", "node-1", "sess-1", 60000, 20000, 1, now=now)

    with pytest.raises(NodeIdentityConflictError, match="active_identity"):
        store.acquire_lease(
            "site-1", "node-1", "sess-2", 60000, 20000, 1, now=now + timedelta(seconds=1)
        )

    assert store.get_node_lease("node-1") == first


def test_stale_replacement_increments_generation_and_fences_old_owner(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire_lease("site-1", "node-1", "sess-1", 1000, 200, 1, now=now)
    replacement = store.acquire_lease(
        "site-1", "node-1", "sess-2", 1000, 200, 1, now=first.expires_at
    )

    assert replacement.generation == 2
    assert replacement.lease_id != first.lease_id
    renewed, reason = store.renew_lease("node-1", "sess-1", first.lease_id, now=first.expires_at)
    assert renewed is None
    assert reason == "invalid_session"


def test_fresh_legacy_record_requires_bounded_stop_and_wait(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    store.upsert_node("node-1", name="legacy")
    store.record_heartbeat("node-1", "Ready")
    node = store.get_node("node-1")
    assert node is not None and node[1] is not None
    seen_at = node[1].seen_at

    with pytest.raises(NodeIdentityConflictError, match="legacy_identity_active"):
        store.acquire_lease(
            "", "node-1", "sess-1", 40000, 13000, 0, now=seen_at + timedelta(seconds=1)
        )

    lease = store.acquire_lease(
        "", "node-1", "sess-1", 40000, 13000, 0, now=seen_at + timedelta(seconds=40)
    )
    assert lease.generation == 1


def test_explicit_heartbeat_requires_matching_fence(tmp_path: Path) -> None:
    store = SQLiteStateStore(db_path=tmp_path / "controller.db")
    store.upsert_node("node-1", name="explicit")
    lease = store.acquire_lease("", "node-1", "sess-1", 60000, 20000, 0)

    refreshed = store.record_heartbeat(
        "node-1", "Ready", session_id="sess-1", lease_id=lease.lease_id
    )
    assert refreshed is not None
    status = store.get_node("node-1")
    assert status is not None and status[1] is not None
    assert status[1].identity_version == 1
    assert status[1].generation == 1

    with pytest.raises(NodeIdentityConflictError, match="identity_fence_required"):
        store.record_heartbeat("node-1", "Ready")
