from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest

from ae.controller.etcd_state import EtcdStateStore
from ae.controller.inference_cell import InferenceCellController, InferenceCellSetController
from ae.controller.spec import InferenceCellManifest, InferenceCellSetManifest


def _b64decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _cell_manifest(name: str = "demo-cell") -> InferenceCellManifest:
    return InferenceCellManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {
                "model": {"modelId": "llama", "localPath": "/models/llama"},
                "parallelism": {"tp": 1, "pp": 2},
                "members": [
                    {"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1},
                    {"siteId": "site-b", "nodeId": "node-b", "gpuCount": 1},
                ],
                "linkMetrics": [
                    {
                        "fromSite": "site-a",
                        "toSite": "site-b",
                        "rttP95Ms": 5.0,
                        "jitterP95Ms": 0.2,
                        "lossPct": 0.0,
                    }
                ],
            },
        }
    )


class _FakeEtcdClient:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int, int]] = {}
        self.mod_revision = 0

    def range(
        self,
        key: str,
        *,
        range_end: str | bytes | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        items = list(self._iter_range(key, range_end=range_end))
        if limit is not None:
            items = items[: int(limit)]
        return {
            "kvs": [
                self._kv(key, value, create_rev, mod_rev)
                for key, value, create_rev, mod_rev in items
            ]
        }

    def put(self, key: str, value: str, *, lease: int | None = None) -> None:
        del lease
        create_rev = self.store[key][1] if key in self.store else 0
        self.mod_revision += 1
        if create_rev == 0:
            create_rev = self.mod_revision
        self.store[key] = (value, create_rev, self.mod_revision)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        for key in list(self.store):
            if key.startswith(prefix):
                self.store.pop(key, None)

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> dict:
        succeeded = self._matches(compare)
        requests = success if succeeded else failure
        responses: list[dict[str, object]] = []
        for request in requests:
            if "requestPut" in request:
                put_req = request["requestPut"]
                assert isinstance(put_req, dict)
                self.put(_b64decode(str(put_req["key"])), _b64decode(str(put_req["value"])))
            elif "requestDeleteRange" in request:
                delete_req = request["requestDeleteRange"]
                assert isinstance(delete_req, dict)
                self.delete(_b64decode(str(delete_req["key"])))
            elif "requestRange" in request:
                range_req = request["requestRange"]
                assert isinstance(range_req, dict)
                key = _b64decode(str(range_req["key"]))
                limit = int(range_req.get("limit", 0) or 0) or None
                responses.append(self.range(key, limit=limit))
        return {"succeeded": succeeded, "responses": responses}

    def grant_lease(self, _ttl_seconds: int) -> int:
        self.mod_revision += 1
        return 10_000 + self.mod_revision

    def _matches(self, compares: list[dict]) -> bool:
        for compare in compares:
            key = _b64decode(str(compare["key"]))
            target = str(compare["target"]).upper()
            current = self.store.get(key)
            if target == "CREATE":
                create_rev = current[1] if current else 0
                if create_rev != int(compare["createRevision"]):
                    return False
                continue
            if target == "MOD":
                mod_rev = current[2] if current else 0
                if mod_rev != int(compare["modRevision"]):
                    return False
                continue
            raise AssertionError(f"unsupported compare target: {target}")
        return True

    def _iter_range(
        self, key: str, *, range_end: str | bytes | None = None
    ) -> Iterator[tuple[str, str, int, int]]:
        keys = sorted(self.store)
        if range_end is None:
            current = self.store.get(key)
            if current is None:
                return iter(())
            return iter([(key, current[0], current[1], current[2])])
        start_bytes = key.encode("utf-8")
        if isinstance(range_end, bytes):
            end_bytes = range_end
        else:
            end_bytes = str(range_end).encode("utf-8")
        items = []
        for current_key in keys:
            current_bytes = current_key.encode("utf-8")
            if current_bytes < start_bytes:
                continue
            if end_bytes and current_bytes >= end_bytes:
                continue
            value, create_rev, mod_rev = self.store[current_key]
            items.append((current_key, value, create_rev, mod_rev))
        return iter(items)

    @staticmethod
    def _kv(key: str, value: str, create_rev: int, mod_rev: int) -> dict[str, object]:
        return {
            "key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
            "value": base64.b64encode(value.encode("utf-8")).decode("ascii"),
            "create_revision": str(create_rev),
            "mod_revision": str(mod_rev),
        }


def _mk_store() -> EtcdStateStore:
    store = object.__new__(EtcdStateStore)
    store._client = _FakeEtcdClient()  # type: ignore[attr-defined]
    store._prefix = "k1s/test"  # type: ignore[attr-defined]
    store._site_id = "core"  # type: ignore[attr-defined]
    store._lease_ttl_seconds = 60  # type: ignore[attr-defined]
    store._lease_refresh_ratio = 0.5  # type: ignore[attr-defined]
    store._node_leases = {}  # type: ignore[attr-defined]
    store.backend = "etcd"  # type: ignore[attr-defined]
    return store


def test_etcd_store_supports_inference_cell_controller_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AE_INFERENCE_EXPERIMENTAL", raising=False)
    store = _mk_store()
    ctrl = InferenceCellController(store)

    rec = ctrl.reconcile_manifest(_cell_manifest(), source="test")

    assert rec.phase == "READY"
    assert rec.allocations.get("fabric_session_id")
    assert store.get_inference_cell("demo-cell", namespace="default") is not None
    events = store.list_inference_cell_events("demo-cell", namespace="default")
    assert any(event.event_type == "CellReady" for event in events)
    sessions = store.list_fabric_sessions(cell_name="demo-cell", namespace="default")
    assert len(sessions) == 1

    ctrl.delete_cell("demo-cell", namespace="default")

    assert store.get_inference_cell("demo-cell", namespace="default") is None
    assert store.list_inference_cell_events("demo-cell", namespace="default") == []
    assert store.list_fabric_sessions(cell_name="demo-cell", namespace="default") == []


def test_etcd_store_supports_inference_cellset_scale_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AE_INFERENCE_EXPERIMENTAL", raising=False)
    store = _mk_store()
    ctrl = InferenceCellSetController(store)
    manifest = InferenceCellSetManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCellSet",
            "metadata": {"name": "set-a", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "template": _cell_manifest().spec.model_dump(by_alias=True),
            },
        }
    )

    rec = ctrl.reconcile_manifest(manifest, source="test")
    assert rec.desired == 1
    assert rec.current >= 1

    scaled = ctrl.scale("set-a", 0, namespace="default")

    assert scaled is not None
    assert scaled.desired == 0
    cells = [
        cell
        for cell in store.list_inference_cells(namespace="default")
        if (cell.manifest.metadata.labels or {}).get("k1s.cellset") == "set-a"
    ]
    assert cells == []

    ctrl.delete_cellset("set-a", namespace="default")

    assert store.get_inference_cellset("set-a", namespace="default") is None
