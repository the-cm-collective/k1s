from __future__ import annotations

import json
from types import SimpleNamespace

from ae.controller.authority import LeaderInfo
from ae.transport.controller_ingress import NatsControllerIngress
from ae.transport.nats_client import NatsMessage
from ae.transport.outbox_publisher import OutboxPublisher
from ae.transport.route_bundle_publisher import RouteBundlePublisher


class _FakeAuthority:
    def __init__(self, *, is_leader: bool, epoch: int = 9) -> None:
        self.is_leader = is_leader
        self.epoch = epoch

    def snapshot(self):
        return SimpleNamespace(
            is_leader=self.is_leader,
            leader_info=LeaderInfo(
                controller_id="ctrl-a" if self.is_leader else "ctrl-b",
                controller_epoch=self.epoch,
                lease_id=501,
                advertise_addr="http://ctrl-a:9108" if self.is_leader else "http://ctrl-b:9108",
                acquired_at=None,
                version="v1",
            ),
        )


class _FakeNatsClient:
    def __init__(self, *args, **kwargs) -> None:
        self.subscriptions: list[tuple[str, object]] = []
        self.unsubscribed: list[str] = []
        self.published: list[tuple[str, dict]] = []
        self.published_js: list[tuple[str, dict, dict | None]] = []

    def connect(self) -> None:
        return None

    def subscribe(self, subject, callback):
        sid = f"sid-{len(self.subscriptions) + 1}"
        self.subscriptions.append((subject, callback))
        return sid

    def unsubscribe(self, sid: str) -> None:
        self.unsubscribed.append(sid)

    def publish_json(self, subject: str, payload: dict, headers=None) -> None:
        self.published.append((subject, payload))

    def publish_js_json(self, subject: str, payload: dict, headers=None) -> None:
        self.published_js.append((subject, payload, headers))

    def close(self) -> None:
        return None


def test_nats_controller_ingress_only_subscribes_when_leader(monkeypatch) -> None:
    monkeypatch.setattr("ae.transport.controller_ingress.NatsClient", _FakeNatsClient)
    authority = _FakeAuthority(is_leader=False)
    ingress = NatsControllerIngress(
        SimpleNamespace(),
        url="nats://127.0.0.1:4222",
        authority=authority,
    )

    ingress.start()
    assert ingress._subs == []  # type: ignore[attr-defined]

    authority.is_leader = True
    ingress.sync_authority()
    assert len(ingress._subs) == 5  # type: ignore[attr-defined]

    authority.is_leader = False
    ingress.sync_authority()
    assert ingress._subs == []  # type: ignore[attr-defined]
    assert len(ingress._client.unsubscribed) == 5  # type: ignore[attr-defined]
    ingress.close()


def test_nats_controller_ingress_replies_not_leader(monkeypatch) -> None:
    monkeypatch.setattr("ae.transport.controller_ingress.NatsClient", _FakeNatsClient)
    authority = _FakeAuthority(is_leader=False, epoch=12)
    ingress = NatsControllerIngress(
        SimpleNamespace(),
        url="nats://127.0.0.1:4222",
        authority=authority,
    )

    ingress._on_work_pull(  # type: ignore[attr-defined]
        NatsMessage(
            subject="k1s.v1.site.sea.work.pull",
            reply="reply.inbox",
            data=json.dumps({"site_id": "sea"}).encode("utf-8"),
        )
    )

    assert ingress._client.published  # type: ignore[attr-defined]
    _subject, payload = ingress._client.published[0]  # type: ignore[attr-defined]
    assert payload["reason"] == "not_leader"
    assert payload["controller_epoch"] == 12
    assert payload["controller_id"] == "ctrl-b"


def test_outbox_publisher_skips_publish_until_leader(monkeypatch) -> None:
    monkeypatch.setattr("ae.transport.outbox_publisher.NatsClient", _FakeNatsClient)
    authority = _FakeAuthority(is_leader=False)
    calls = {"published": 0, "state_updates": 0}

    class _Store:
        def list_outbox_unpublished(self, limit: int):
            return [SimpleNamespace(work_id="w1", attempt=1, site_id="sea", payload={"ok": True})]

        def mark_outbox_published(self, work_id: str, attempt: int) -> None:
            calls["published"] += 1

        def update_work_state(self, **kwargs) -> None:
            calls["state_updates"] += 1

        def record_outbox_publish_attempt(self, work_id: str, attempt: int) -> None:
            raise AssertionError("should not record failure in this test")

    publisher = OutboxPublisher(
        _Store(),
        nats_url="nats://127.0.0.1:4222",
        authority=authority,
    )

    publisher.run_once()
    assert publisher._client.published_js == []  # type: ignore[attr-defined]
    assert calls["published"] == 0

    authority.is_leader = True
    publisher.run_once()
    assert len(publisher._client.published_js) == 1  # type: ignore[attr-defined]
    assert calls["published"] == 1
    assert calls["state_updates"] == 1


def test_route_bundle_publisher_skips_publish_until_leader(monkeypatch) -> None:
    monkeypatch.setattr("ae.transport.route_bundle_publisher.NatsClient", _FakeNatsClient)
    authority = _FakeAuthority(is_leader=False)

    class _Store:
        def list_route_bundle_site_ids(self):
            return ["sea-edge-02"]

        def list_edge_ingress_routes_for_site(self, site_id: str):
            return []

        def get_edge_ingress_policy(self, *, name: str, namespace: str):
            return None

        def list_service_endpoints(self, app_name: str):
            return []

    publisher = RouteBundlePublisher(
        _Store(),
        nats_url="nats://127.0.0.1:4222",
        authority=authority,
    )

    publisher.run_once()
    assert publisher._client.published == []  # type: ignore[attr-defined]

    authority.is_leader = True
    publisher.run_once()
    assert len(publisher._client.published) == 1  # type: ignore[attr-defined]
