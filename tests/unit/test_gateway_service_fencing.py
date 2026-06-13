from __future__ import annotations

import json
from pathlib import Path

from ae.config.transport import GatewayJetStreamConfig
from ae.gateway.service import SiteGateway
from ae.ha.fencing import MutationEnvelope
from ae.transport.nats_client import NatsMessage
from ae.transport.subjects import hub_route_bundle_subject


class _FakeNatsClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, dict]] = []
        self.request_responses: list[dict] = []
        self.listeners: list[object] = []
        self.subscriptions: list[tuple[str, object]] = []
        self.connect_attempts = 0
        self.connected = False
        self.fail_result_publish = False

    def connect(self) -> None:
        self.connect_attempts += 1
        self.connected = True

    def publish_json(self, subject: str, payload: dict, headers=None) -> None:
        if self.fail_result_publish and subject == "k1s.v1.site.sea.result":
            raise RuntimeError("publish failed")
        self.published.append((subject, payload))

    def request_json(self, subject: str, payload: dict, timeout_s: float | None = None):
        self.requests.append((subject, payload))
        if not self.request_responses:
            raise AssertionError("unexpected request")
        return self.request_responses.pop(0)

    def add_reconnect_listener(self, callback) -> None:
        self.listeners.append(callback)

    def subscribe(self, subject, callback):
        self.subscriptions.append((subject, callback))
        return "sid-1"

    def close(self) -> None:
        self.connected = False
        return None


class _FlakyConnectNatsClient(_FakeNatsClient):
    def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_attempts == 1:
            self.connected = False
            raise RuntimeError("nats unavailable")
        self.connected = True


class _Renderer:
    def __init__(self) -> None:
        self.applied: list[dict] = []

    def apply_bundle(self, payload: dict):
        self.applied.append(payload)
        return True, None


def _gateway(tmp_path: Path, *, nats: _FakeNatsClient | None = None) -> SiteGateway:
    cfg = GatewayJetStreamConfig(
        ack_wait="30s",
        progress_interval="10s",
        progress_jitter_pct=15,
        max_ack_pending=32,
        max_deliver=20,
        max_waiting=512,
        spool_path=tmp_path / "spool.db",
    )
    gateway = SiteGateway(
        site_id="sea",
        node_id="node-a",
        nats_url="nats://127.0.0.1:4222",
        js_config=cfg,
        status_interval_s=30,
        nats_client=nats or _FakeNatsClient(),
    )
    gateway._edge_local_renderer = _Renderer()  # type: ignore[attr-defined]
    gateway._fence.init()  # type: ignore[attr-defined]
    gateway._spool.init()  # type: ignore[attr-defined]
    return gateway


def test_gateway_route_bundle_rejects_stale_epoch_across_restart(tmp_path: Path) -> None:
    first = _gateway(tmp_path)
    first._on_route_bundle(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.routes.bundle",
            reply=None,
            data=json.dumps(
                {
                    "site_id": "sea",
                    "bundle_rev": 1,
                    "hash": "sha256:first",
                    "controller_id": "ctrl-a",
                    "controller_epoch": 9,
                    "operation_id": "route:sea:1:9",
                }
            ).encode("utf-8"),
        )
    )
    assert first._edge_local_renderer.applied  # type: ignore[attr-defined]

    second = _gateway(tmp_path)
    second._on_route_bundle(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.routes.bundle",
            reply=None,
            data=json.dumps(
                {
                    "site_id": "sea",
                    "bundle_rev": 1,
                    "hash": "sha256:stale",
                    "controller_id": "ctrl-old",
                    "controller_epoch": 8,
                    "operation_id": "route:sea:1:8",
                }
            ).encode("utf-8"),
        )
    )
    assert second._edge_local_renderer.applied == []  # type: ignore[attr-defined]


def test_gateway_route_bundle_resets_revision_on_epoch_advance(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    gateway._route_bundle_rev = 4  # type: ignore[attr-defined]
    gateway._route_bundle_hash = "sha256:old"  # type: ignore[attr-defined]
    gateway._fence.commit(  # type: ignore[attr-defined]
        "site:sea",
        MutationEnvelope("ctrl-old", 8, "route:sea:4:8"),
    )

    gateway._on_route_bundle(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.routes.bundle",
            reply=None,
            data=json.dumps(
                {
                    "site_id": "sea",
                    "bundle_rev": 1,
                    "hash": "sha256:new",
                    "controller_id": "ctrl-new",
                    "controller_epoch": 9,
                    "operation_id": "route:sea:1:9",
                }
            ).encode("utf-8"),
        )
    )

    assert [item["hash"] for item in gateway._edge_local_renderer.applied] == [  # type: ignore[attr-defined]
        "sha256:new"
    ]
    assert gateway._route_bundle_rev == 1  # type: ignore[attr-defined]
    assert gateway._route_bundle_hash == "sha256:new"  # type: ignore[attr-defined]


def test_gateway_route_bundle_duplicate_reapplies_after_restart(tmp_path: Path) -> None:
    payload = {
        "site_id": "sea",
        "bundle_rev": 1,
        "hash": "sha256:current",
        "controller_id": "ctrl-a",
        "controller_epoch": 9,
        "operation_id": "route:sea:1:9",
    }

    first = _gateway(tmp_path)
    first._on_route_bundle(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.routes.bundle",
            reply=None,
            data=json.dumps(payload).encode("utf-8"),
        )
    )
    assert first._edge_local_renderer.applied  # type: ignore[attr-defined]

    second = _gateway(tmp_path)
    second._on_route_bundle(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.routes.bundle",
            reply=None,
            data=json.dumps(payload).encode("utf-8"),
        )
    )

    assert [item["hash"] for item in second._edge_local_renderer.applied] == [  # type: ignore[attr-defined]
        "sha256:current"
    ]
    assert second._route_bundle_rev == 1  # type: ignore[attr-defined]
    assert second._route_bundle_hash == "sha256:current"  # type: ignore[attr-defined]


def test_gateway_work_pull_ack_includes_envelope_for_stale_and_accepted_items(tmp_path: Path) -> None:
    nats = _FakeNatsClient()
    gateway = _gateway(tmp_path, nats=nats)
    gateway._fence.commit(  # type: ignore[attr-defined]
        "site:sea",
        MutationEnvelope("ctrl-b", 10, "route:sea:1:10"),
    )
    nats.request_responses.append(
        {
            "accepted": True,
            "work": [
                {
                    "work_id": "w-stale",
                    "attempt": 1,
                    "controller_id": "ctrl-a",
                    "controller_epoch": 9,
                    "operation_id": "work:w-stale:1",
                },
                {
                    "work_id": "w-fresh",
                    "attempt": 1,
                    "controller_id": "ctrl-b",
                    "controller_epoch": 10,
                    "operation_id": "work:w-fresh:1",
                },
            ],
            "lease_ids": ["lease-stale", "lease-fresh"],
        }
    )
    nats.request_responses.append({"accepted": True})

    gateway._poll_work_pull(100.0)  # type: ignore[attr-defined]

    local_subjects = [subject for subject, _payload in nats.published]
    assert local_subjects == ["k1s.v1.local.work.node-a", "k1s.v1.site.sea.result"]
    ack_subject, ack_payload = nats.requests[-1]
    assert ack_subject == "k1s.v1.site.sea.work.ack"
    assert [item["lease_id"] for item in ack_payload["ack_items"]] == ["lease-stale", "lease-fresh"]
    assert ack_payload["ack_items"][0]["operation_id"] == "work:w-stale:1"
    assert ack_payload["ack_items"][1]["operation_id"] == "work:w-fresh:1"


def test_gateway_reuses_lease_request_id_after_retry(tmp_path: Path) -> None:
    nats = _FakeNatsClient()
    gateway = _gateway(tmp_path, nats=nats)

    def _failing_request(subject: str, payload: dict, timeout_s: float | None = None):
        nats.requests.append((subject, payload))
        if len(nats.requests) == 1:
            raise RuntimeError("timeout")
        return {
            "accepted": True,
            "lease_id": "lease-1",
            "lease_ttl_ms": 60000,
            "renew_after_ms": 20000,
            "controller_id": "ctrl-a",
            "controller_epoch": 11,
            "operation_id": payload["request_id"],
        }

    nats.request_json = _failing_request  # type: ignore[method-assign]

    assert gateway._acquire_lease(1.0) is False  # type: ignore[attr-defined]
    assert gateway._acquire_lease(2.0) is True  # type: ignore[attr-defined]
    assert nats.requests[0][1]["request_id"] == nats.requests[1][1]["request_id"]


def test_gateway_replay_failure_schedules_backoff(tmp_path: Path) -> None:
    nats = _FakeNatsClient()
    gateway = _gateway(tmp_path, nats=nats)
    gateway._spool.record_result(  # type: ignore[attr-defined]
        "w1",
        1,
        "failed",
        {
            "work_id": "w1",
            "attempt": 1,
            "status": "failed",
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "work:w1:1",
        },
    )
    nats.fail_result_publish = True

    gateway._replay_spool_results(100.0)  # type: ignore[attr-defined]

    record = gateway._spool.get_result("w1", 1)  # type: ignore[attr-defined]
    assert record is not None
    assert record.delivered_to_controller_at is None
    assert record.replay_attempts == 1
    assert record.next_retry_at is not None
    assert gateway._stats.result_replay_fail_total == 1  # type: ignore[attr-defined]


def test_gateway_reconnect_resets_replay_schedule(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    gateway = _gateway(tmp_path)
    gateway._spool.record_result(  # type: ignore[attr-defined]
        "w1",
        1,
        "succeeded",
        {
            "work_id": "w1",
            "attempt": 1,
            "status": "succeeded",
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "work:w1:1",
        },
    )
    gateway._spool.record_result_delivery_attempt(  # type: ignore[attr-defined]
        "w1",
        1,
        error="nats down",
        retry_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert gateway._spool.list_replay_ready_results() == []  # type: ignore[attr-defined]

    gateway._on_transport_reconnect()  # type: ignore[attr-defined]

    ready = gateway._spool.list_replay_ready_results()  # type: ignore[attr-defined]
    assert len(ready) == 1
    assert ready[0].work_id == "w1"


def test_gateway_replays_buffered_result_after_restart(tmp_path: Path) -> None:
    first = _gateway(tmp_path)
    first._spool.record_result(  # type: ignore[attr-defined]
        "w1",
        1,
        "succeeded",
        {
            "work_id": "w1",
            "attempt": 1,
            "status": "succeeded",
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "work:w1:1",
        },
    )

    nats = _FakeNatsClient()
    second = _gateway(tmp_path, nats=nats)
    second._replay_spool_results(100.0)  # type: ignore[attr-defined]

    assert nats.published == [
        (
            "k1s.v1.site.sea.result",
            {
                "work_id": "w1",
                "attempt": 1,
                "status": "succeeded",
                "controller_id": "ctrl-a",
                "controller_epoch": 7,
                "operation_id": "work:w1:1",
            },
        )
    ]
    record = second._spool.get_result("w1", 1)  # type: ignore[attr-defined]
    assert record is not None
    assert record.delivered_to_controller_at is not None


def test_gateway_retries_initial_nats_connect_and_subscribes_routes(tmp_path: Path) -> None:
    nats = _FlakyConnectNatsClient()
    gateway = _gateway(tmp_path, nats=nats)

    assert gateway._ensure_transport_connected(10.0) is False  # type: ignore[attr-defined]
    assert nats.subscriptions == []

    assert gateway._ensure_transport_connected(11.0) is True  # type: ignore[attr-defined]

    assert nats.connect_attempts == 2
    assert [subject for subject, _callback in nats.subscriptions] == [
        "k1s.v1.local.result",
        "k1s.v1.local.work.progress",
        hub_route_bundle_subject("sea"),
    ]
