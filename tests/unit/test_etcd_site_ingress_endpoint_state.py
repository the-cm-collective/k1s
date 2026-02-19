from __future__ import annotations

import copy

from ae.controller.etcd_state import EtcdStateStore


def _build_store(initial_records: dict[str, dict]):
    store = object.__new__(EtcdStateStore)
    store._prefix = "k1s/test"  # type: ignore[attr-defined]

    records = copy.deepcopy(initial_records)

    def _get_json(key: str):
        rec = records.get(key)
        if rec is None:
            return None, 0
        return copy.deepcopy(rec), 1

    def _put_json(key: str, payload: dict, *, lease_id: int | None = None) -> None:  # noqa: ARG001
        records[key] = copy.deepcopy(payload)

    def _list_prefix(prefix: str):
        out = []
        for key, rec in records.items():
            if key.startswith(prefix):
                out.append((key, copy.deepcopy(rec), 1))
        return out

    store._get_json = _get_json  # type: ignore[method-assign]
    store._put_json = _put_json  # type: ignore[method-assign]
    store._list_prefix = _list_prefix  # type: ignore[method-assign]
    return store, records


def test_ensure_site_ingress_port_preserves_public_endpoint_metadata() -> None:
    site_key = "k1s/test/ingress/sites/sea-edge-02"
    initial = {
        "site_id": "sea-edge-02",
        "mode": "core-to-edge-public",
        "core_proxy_port": None,
        "public_urls": [
            {
                "url": "https://pop-sea-edge-02.home.arpa:11443",
                "expectedSANs": ["pop-sea-edge-02.home.arpa"],
            }
        ],
        "quarantine_until": None,
        "created_at": "2026-02-17T00:00:00+00:00",
        "updated_at": "2026-02-17T00:00:00+00:00",
    }
    store, records = _build_store({site_key: initial})

    port = store.ensure_site_ingress_port(
        "sea-edge-02", port_min=18080, port_max=18080, mode="core-proxy"
    )

    assert port == 18080
    updated = records[site_key]
    assert updated["core_proxy_port"] == 18080
    assert updated["public_urls"] == initial["public_urls"]
    assert updated["created_at"] == initial["created_at"]


def test_upsert_site_ingress_endpoint_preserves_existing_core_proxy_port() -> None:
    site_key = "k1s/test/ingress/sites/sea-edge-02"
    initial = {
        "site_id": "sea-edge-02",
        "mode": "core-proxy",
        "core_proxy_port": 18080,
        "public_urls": [],
        "quarantine_until": None,
        "created_at": "2026-02-17T00:00:00+00:00",
        "updated_at": "2026-02-17T00:00:00+00:00",
    }
    store, records = _build_store({site_key: initial})

    store.upsert_site_ingress_endpoint(
        site_id="sea-edge-02",
        mode="core-to-edge-public",
        core_proxy_port=None,
        public_urls=[{"url": "https://pop-sea-edge-02.home.arpa:11443"}],
    )

    updated = records[site_key]
    assert updated["mode"] == "core-to-edge-public"
    assert updated["core_proxy_port"] == 18080
    assert updated["public_urls"] == [{"url": "https://pop-sea-edge-02.home.arpa:11443"}]
    assert updated["created_at"] == initial["created_at"]
