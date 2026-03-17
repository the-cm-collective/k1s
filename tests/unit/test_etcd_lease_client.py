from __future__ import annotations

from types import SimpleNamespace

import pytest

from ae.controller import etcd_lease_client


class _FakeUnaryRpc:
    def __init__(self, path: str, responder) -> None:
        self.path = path
        self._responder = responder
        self.calls: list[dict] = []

    def __call__(self, payload, *, timeout=None, metadata=None):
        self.calls.append({"payload": payload, "timeout": timeout, "metadata": metadata})
        return self._responder(payload)


class _FakeStreamRpc:
    def __init__(self, path: str, responder) -> None:
        self.path = path
        self._responder = responder
        self.calls: list[dict] = []

    def __call__(self, payloads, *, timeout=None, metadata=None):
        payloads = list(payloads)
        self.calls.append({"payloads": payloads, "timeout": timeout, "metadata": metadata})
        return iter(self._responder(payloads))


class _FakeChannel:
    def __init__(self) -> None:
        self.grant = _FakeUnaryRpc(
            "/etcdserverpb.Lease/LeaseGrant",
            lambda _payload: (
                etcd_lease_client._encode_varint((2 << 3) | 0)
                + etcd_lease_client._encode_varint(915)
                + etcd_lease_client._encode_varint((3 << 3) | 0)
                + etcd_lease_client._encode_varint(15)
            ),
        )
        self.keepalive = _FakeStreamRpc(
            "/etcdserverpb.Lease/LeaseKeepAlive",
            lambda _payloads: [
                (
                    etcd_lease_client._encode_varint((2 << 3) | 0)
                    + etcd_lease_client._encode_varint(915)
                    + etcd_lease_client._encode_varint((3 << 3) | 0)
                    + etcd_lease_client._encode_varint(12)
                )
            ],
        )
        self.revoke = _FakeUnaryRpc(
            "/etcdserverpb.Lease/LeaseRevoke",
            lambda _payload: b"",
        )
        self.closed = False

    def unary_unary(self, path, **_kwargs):
        if path.endswith("LeaseGrant"):
            return self.grant
        if path.endswith("LeaseRevoke"):
            return self.revoke
        raise AssertionError(path)

    def stream_stream(self, path, **_kwargs):
        if path.endswith("LeaseKeepAlive"):
            return self.keepalive
        raise AssertionError(path)

    def close(self):
        self.closed = True


def test_grpc_etcd_lease_client_uses_expected_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _FakeChannel()
    fake_grpc = SimpleNamespace(
        insecure_channel=lambda target: channel,
        secure_channel=lambda target, creds: channel,
        ssl_channel_credentials=lambda **_kwargs: object(),
    )
    monkeypatch.setattr(etcd_lease_client, "grpc", fake_grpc)

    client = etcd_lease_client.GrpcEtcdLeaseClient(
        ["http://127.0.0.1:2379"],
        timeout_s=4.0,
        token_provider=lambda: "secret-token",
    )

    lease_id = client.grant_lease(15)
    keepalive = client.keepalive(lease_id)
    client.revoke_lease(lease_id)
    client.close()

    assert lease_id == 915
    assert keepalive.lease_id == 915
    assert keepalive.ttl_seconds == 12
    assert channel.grant.calls[0]["metadata"] == [("token", "secret-token")]
    assert channel.keepalive.calls[0]["metadata"] == [("token", "secret-token")]
    assert channel.revoke.calls[0]["metadata"] == [("token", "secret-token")]
    assert channel.closed is True


def test_normalize_grpc_target_strips_http_scheme() -> None:
    assert etcd_lease_client._normalize_grpc_target("http://127.0.0.1:2379") == ("127.0.0.1:2379", False)
    assert etcd_lease_client._normalize_grpc_target("https://etcd.example:2379") == ("etcd.example:2379", True)
