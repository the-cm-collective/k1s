from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ae.controller.etcd_state import EtcdStateStore, _b64decode, _b64encode
from ae.controller.node_identity import NodeIdentityConflictError


class _FakeEtcdClient:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.revision = 0
        self.fail_next_txn = False

    def range(
        self,
        key: str,
        *,
        range_end: bytes | str | None = None,
        limit: int | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        end = range_end.decode() if isinstance(range_end, bytes) else range_end
        keys = sorted(item for item in self.values if item >= key and (end is None or item < end))
        if limit is not None:
            keys = keys[:limit]
        return {
            "kvs": [
                {
                    "key": _b64encode(item),
                    "value": _b64encode(json.dumps(self.values[item], sort_keys=True)),
                    "mod_revision": str(self.revisions[item]),
                }
                for item in keys
            ]
        }

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> dict:
        del failure
        matched = not self.fail_next_txn
        self.fail_next_txn = False
        for condition in compare:
            key = _b64decode(condition["key"]).decode()
            if condition["target"] == "CREATE":
                matched = matched and key not in self.values
            elif condition["target"] == "MOD":
                matched = matched and self.revisions.get(key, 0) == int(condition["modRevision"])
        if matched:
            for operation in success:
                put = operation["requestPut"]
                key = _b64decode(put["key"]).decode()
                value = json.loads(_b64decode(put["value"]).decode())
                self.revision += 1
                self.values[key] = value
                self.revisions[key] = self.revision
        return {"succeeded": matched}


def _store(client: _FakeEtcdClient) -> EtcdStateStore:
    store = object.__new__(EtcdStateStore)
    store._client = client  # type: ignore[attr-defined]
    store._prefix = "k1s/v1"  # type: ignore[attr-defined]
    store._site_id = "core"  # type: ignore[attr-defined]
    store._read_timeout_s = 10.0  # type: ignore[attr-defined]
    return store


def test_etcd_acquire_is_idempotent_and_rejects_active_duplicate() -> None:
    store = _store(_FakeEtcdClient())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire_lease("site-a", "site-a--node-1", "sess-1", 60000, 20000, 1, now=now)
    retry = store.acquire_lease(
        "site-a", "site-a--node-1", "sess-1", 60000, 20000, 1, now=now + timedelta(seconds=1)
    )

    assert retry.lease_id == first.lease_id
    assert retry.generation == 1
    with pytest.raises(NodeIdentityConflictError, match="active_identity"):
        store.acquire_lease(
            "site-a",
            "site-a--node-1",
            "sess-2",
            60000,
            20000,
            1,
            now=now + timedelta(seconds=2),
        )


def test_etcd_stale_replacement_uses_mod_revision_cas() -> None:
    client = _FakeEtcdClient()
    store = _store(client)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = store.acquire_lease("site-a", "site-a--node-1", "sess-1", 1000, 200, 1, now=now)
    replacement = store.acquire_lease(
        "site-a", "site-a--node-1", "sess-2", 1000, 200, 1, now=first.expires_at
    )

    assert replacement.generation == 2
    renewed, reason = store.renew_lease(
        "site-a--node-1", "sess-1", first.lease_id, now=first.expires_at
    )
    assert renewed is None
    assert reason == "invalid_session"


def test_etcd_cas_loser_fails_closed() -> None:
    client = _FakeEtcdClient()
    store = _store(client)
    client.fail_next_txn = True

    with pytest.raises(NodeIdentityConflictError, match="active_identity"):
        store.acquire_lease(
            "site-a",
            "site-a--node-1",
            "sess-1",
            60000,
            20000,
            1,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
