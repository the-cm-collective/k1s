from __future__ import annotations

import base64
import json

import pytest

from ae.controller.authority import AuthorityConfig, ControllerAuthorityService


class _FakeKeepAliveResult:
    def __init__(self, lease_id: int, ttl_seconds: int = 15) -> None:
        self.lease_id = lease_id
        self.ttl_seconds = ttl_seconds


class _FakeLeaseClient:
    def __init__(self) -> None:
        self._next_id = 500
        self.fail_keepalive: set[int] = set()
        self.revoked: list[int] = []

    def grant_lease(self, ttl_seconds: int, *, lease_id: int = 0) -> int:
        if lease_id:
            return lease_id
        self._next_id += 1
        return self._next_id

    def keepalive(self, lease_id: int) -> _FakeKeepAliveResult:
        if lease_id in self.fail_keepalive:
            raise RuntimeError("keepalive failed")
        return _FakeKeepAliveResult(lease_id=lease_id)

    def revoke_lease(self, lease_id: int) -> None:
        self.revoked.append(lease_id)

    def close(self) -> None:
        return None


class _FakeKvClient:
    def __init__(self) -> None:
        self._rev = 0
        self._records: dict[str, dict[str, object]] = {}

    def put(self, key: str, value: str, *, lease: int | None = None) -> None:
        self._rev += 1
        current = self._records.get(key)
        create_revision = self._rev if current is None else int(current["create_revision"])
        self._records[key] = {
            "value": value,
            "lease": lease,
            "create_revision": create_revision,
            "mod_revision": self._rev,
        }

    def delete(self, key: str) -> None:
        self._records.pop(key, None)

    def range(self, key: str, *, range_end=None, limit: int | None = None) -> dict:
        rec = self._records.get(key)
        if rec is None:
            return {}
        return {
            "kvs": [
                {
                    "key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
                    "value": base64.b64encode(str(rec["value"]).encode("utf-8")).decode("ascii"),
                    "create_revision": rec["create_revision"],
                    "mod_revision": rec["mod_revision"],
                    "lease": rec["lease"] or 0,
                }
            ]
        }

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> dict:
        assert len(compare) == 1
        cmp = compare[0]
        key = base64.b64decode(cmp["key"]).decode("utf-8")
        exists = key in self._records
        if not exists:
            for op in success:
                put = op.get("requestPut")
                if put:
                    self.put(
                        base64.b64decode(put["key"]).decode("utf-8"),
                        base64.b64decode(put["value"]).decode("utf-8"),
                        lease=int(put.get("lease") or 0) or None,
                    )
            return {"succeeded": True}
        responses = []
        for op in failure:
            req = op.get("requestRange")
            if req:
                leader_key = base64.b64decode(req["key"]).decode("utf-8")
                resp = self.range(leader_key, limit=int(req.get("limit") or 0) or None)
                responses.append({"responseRange": resp})
        return {"succeeded": False, "responses": responses}


def _config(**overrides) -> AuthorityConfig:
    base = {
        "enabled": True,
        "controller_id": "ctrl-a",
        "advertise_addr": "http://ctrl-a:9000",
        "etcd_prefix": "k1s/test",
        "lease_ttl_seconds": 15,
        "keepalive_interval_seconds": 5.0,
        "standby_retry_min_seconds": 1.0,
        "standby_retry_max_seconds": 1.0,
        "follower_poll_seconds": 2.0,
        "version": "v1",
    }
    base.update(overrides)
    return AuthorityConfig(**base)


def test_authority_service_claims_leadership_and_derives_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ae.controller.authority.random.uniform", lambda low, high: low)
    kv = _FakeKvClient()
    leases = _FakeLeaseClient()
    service = ControllerAuthorityService(config=_config(), kv_client=kv, lease_client=leases)

    service.run_once(now_monotonic=0.0)
    snapshot = service.snapshot()

    assert snapshot.is_leader is True
    assert snapshot.leader_info is not None
    assert snapshot.leader_info.controller_id == "ctrl-a"
    assert snapshot.leader_info.controller_epoch == 2
    assert snapshot.leader_info.advertise_addr == "http://ctrl-a:9000"


def test_authority_service_stays_standby_when_leader_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ae.controller.authority.random.uniform", lambda low, high: low)
    kv = _FakeKvClient()
    kv.put(
        "k1s/test/controlplane/leader",
        json.dumps(
            {
                "controller_id": "ctrl-b",
                "lease_id": 701,
                "advertise_addr": "http://ctrl-b:9000",
                "acquired_at": "2026-03-17T00:00:00+00:00",
                "version": "v1",
            }
        ),
        lease=701,
    )
    leases = _FakeLeaseClient()
    service = ControllerAuthorityService(config=_config(), kv_client=kv, lease_client=leases)

    service.run_once(now_monotonic=0.0)
    snapshot = service.snapshot()

    assert snapshot.is_leader is False
    assert snapshot.leader_info is not None
    assert snapshot.leader_info.controller_id == "ctrl-b"
    assert snapshot.leader_info.controller_epoch == 1


def test_authority_service_sets_leader_lost_on_keepalive_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ae.controller.authority.random.uniform", lambda low, high: low)
    kv = _FakeKvClient()
    leases = _FakeLeaseClient()
    service = ControllerAuthorityService(config=_config(), kv_client=kv, lease_client=leases)

    service.run_once(now_monotonic=0.0)
    initial = service.snapshot()
    assert initial.leader_info is not None
    leases.fail_keepalive.add(initial.leader_info.lease_id)

    service.run_once(now_monotonic=6.0)
    snapshot = service.snapshot()

    assert snapshot.is_leader is False
    assert service.leader_lost.is_set() is True


def test_authority_service_acquires_after_existing_leader_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ae.controller.authority.random.uniform", lambda low, high: low)
    kv = _FakeKvClient()
    kv.put(
        "k1s/test/controlplane/leader",
        json.dumps(
            {
                "controller_id": "ctrl-b",
                "lease_id": 701,
                "advertise_addr": "http://ctrl-b:9000",
                "acquired_at": "2026-03-17T00:00:00+00:00",
                "version": "v1",
            }
        ),
        lease=701,
    )
    leases = _FakeLeaseClient()
    service = ControllerAuthorityService(config=_config(), kv_client=kv, lease_client=leases)

    service.run_once(now_monotonic=0.0)
    kv.delete("k1s/test/controlplane/leader")
    service.run_once(now_monotonic=3.0)
    snapshot = service.snapshot()

    assert snapshot.is_leader is True
    assert snapshot.leader_info is not None
    assert snapshot.leader_info.controller_id == "ctrl-a"


def test_authority_service_fails_over_between_two_controllers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ae.controller.authority.random.uniform", lambda low, high: low)
    kv = _FakeKvClient()
    leases = _FakeLeaseClient()
    leader = ControllerAuthorityService(config=_config(), kv_client=kv, lease_client=leases)
    standby = ControllerAuthorityService(
        config=_config(controller_id="ctrl-b", advertise_addr="http://ctrl-b:9000"),
        kv_client=kv,
        lease_client=leases,
    )

    leader.run_once(now_monotonic=0.0)
    standby.run_once(now_monotonic=0.0)
    leader_initial = leader.snapshot()
    standby_initial = standby.snapshot()

    assert leader_initial.is_leader is True
    assert leader_initial.leader_info is not None
    assert standby_initial.is_leader is False
    assert standby_initial.leader_info is not None
    assert standby_initial.leader_info.controller_id == "ctrl-a"

    leases.fail_keepalive.add(leader_initial.leader_info.lease_id)
    leader.run_once(now_monotonic=6.0)
    kv.delete("k1s/test/controlplane/leader")
    standby.run_once(now_monotonic=6.0)
    failed_over = standby.snapshot()

    assert leader.leader_lost.is_set() is True
    assert failed_over.is_leader is True
    assert failed_over.leader_info is not None
    assert failed_over.leader_info.controller_id == "ctrl-b"
    assert failed_over.leader_info.controller_epoch > leader_initial.leader_info.controller_epoch
