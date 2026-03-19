from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

try:  # pragma: no cover - optional dependency in some unit test environments
    import grpc
    _GRPC_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - optional dependency
    grpc = None
    _GRPC_IMPORT_ERROR = exc

LOGGER = logging.getLogger(__name__)


def _grpc_required_error() -> RuntimeError:
    message = "grpc is required for etcd lease keepalive"
    if _GRPC_IMPORT_ERROR is None:
        return RuntimeError(f"{message} (install grpcio)")
    detail = str(_GRPC_IMPORT_ERROR).strip()
    if isinstance(_GRPC_IMPORT_ERROR, ModuleNotFoundError):
        return RuntimeError(f"{message} (install grpcio): {detail}")
    return RuntimeError(f"{message}: {detail}")


def _encode_varint(value: int) -> bytes:
    raw = int(value)
    if raw < 0:
        raw += 1 << 64
    out = bytearray()
    while raw > 0x7F:
        out.append((raw & 0x7F) | 0x80)
        raw >>= 7
    out.append(raw & 0x7F)
    return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    idx = offset
    while idx < len(data):
        chunk = data[idx]
        idx += 1
        value |= (chunk & 0x7F) << shift
        if not chunk & 0x80:
            return value, idx
        shift += 7
        if shift >= 64:
            break
    raise ValueError("invalid protobuf varint")


def _skip_wire_value(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, next_offset = _decode_varint(data, offset)
        return next_offset
    if wire_type == 1:
        return offset + 8
    if wire_type == 2:
        size, next_offset = _decode_varint(data, offset)
        return next_offset + size
    if wire_type == 5:
        return offset + 4
    raise ValueError(f"unsupported protobuf wire type {wire_type}")


def _encode_lease_grant_request(ttl_seconds: int, lease_id: int = 0) -> bytes:
    payload = bytearray()
    payload.extend(_encode_varint((1 << 3) | 0))
    payload.extend(_encode_varint(ttl_seconds))
    if lease_id:
        payload.extend(_encode_varint((2 << 3) | 0))
        payload.extend(_encode_varint(lease_id))
    return bytes(payload)


def _decode_lease_grant_response(data: bytes) -> tuple[int, int]:
    lease_id = 0
    ttl = 0
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 2 and wire_type == 0:
            lease_id, offset = _decode_varint(data, offset)
            continue
        if field_num == 3 and wire_type == 0:
            ttl, offset = _decode_varint(data, offset)
            continue
        offset = _skip_wire_value(data, offset, wire_type)
    if lease_id <= 0:
        raise RuntimeError("etcd lease grant failed: missing lease id")
    return lease_id, ttl


def _encode_lease_keepalive_request(lease_id: int) -> bytes:
    payload = bytearray()
    payload.extend(_encode_varint((1 << 3) | 0))
    payload.extend(_encode_varint(lease_id))
    return bytes(payload)


def _decode_lease_keepalive_response(data: bytes) -> tuple[int, int]:
    lease_id = 0
    ttl = 0
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 2 and wire_type == 0:
            lease_id, offset = _decode_varint(data, offset)
            continue
        if field_num == 3 and wire_type == 0:
            ttl, offset = _decode_varint(data, offset)
            continue
        offset = _skip_wire_value(data, offset, wire_type)
    if lease_id <= 0:
        raise RuntimeError("etcd lease keepalive failed: missing lease id")
    return lease_id, ttl


def _encode_lease_revoke_request(lease_id: int) -> bytes:
    payload = bytearray()
    payload.extend(_encode_varint((1 << 3) | 0))
    payload.extend(_encode_varint(lease_id))
    return bytes(payload)


def _decode_lease_revoke_response(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        wire_type = tag & 0x7
        offset = _skip_wire_value(data, offset, wire_type)


def _normalize_grpc_target(endpoint: str) -> tuple[str, bool]:
    parsed = urlparse(endpoint)
    if parsed.scheme:
        secure = parsed.scheme == "https"
        host = parsed.netloc or parsed.path
        return host, secure
    return endpoint.strip(), False


@dataclass(slots=True, frozen=True)
class LeaseKeepAliveResult:
    lease_id: int
    ttl_seconds: int


class GrpcEtcdLeaseClient:
    """Minimal etcd Lease gRPC client.

    This client only implements the subset of the Lease service that HA leader
    election needs: grant, keepalive, and revoke.
    """

    def __init__(
        self,
        endpoints: list[str],
        *,
        ca_cert: str | None = None,
        cert: str | None = None,
        key: str | None = None,
        timeout_s: float = 3.0,
        token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        if grpc is None:  # pragma: no cover - depends on optional dependency
            raise _grpc_required_error()
        clean = [e.strip() for e in endpoints if e and e.strip()]
        if not clean:
            raise ValueError("etcd endpoints required")
        self._targets = [_normalize_grpc_target(endpoint) for endpoint in clean]
        self._timeout_s = float(timeout_s)
        self._token_provider = token_provider
        self._channel = None
        self._channel_target: str | None = None
        self._grant_rpc = None
        self._keepalive_rpc = None
        self._revoke_rpc = None
        self._ca_cert = ca_cert
        self._cert = cert
        self._key = key

    @classmethod
    def from_env(
        cls,
        *,
        token_provider: Callable[[], str | None] | None = None,
        env: dict[str, str] | None = None,
        endpoints: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> "GrpcEtcdLeaseClient":
        env_map = env or os.environ
        raw_endpoints = endpoints or [
            e.strip() for e in (env_map.get("AE_ETCD_ENDPOINTS") or "http://127.0.0.1:2379").split(",")
        ]
        return cls(
            raw_endpoints,
            ca_cert=env_map.get("AE_ETCD_CA") or None,
            cert=env_map.get("AE_ETCD_CERT") or None,
            key=env_map.get("AE_ETCD_KEY") or None,
            timeout_s=float(timeout_s if timeout_s is not None else env_map.get("AE_ETCD_OP_TIMEOUT", "3") or 3),
            token_provider=token_provider,
        )

    def _metadata(self) -> list[tuple[str, str]] | None:
        if self._token_provider is None:
            return None
        token = self._token_provider()
        if not token:
            return None
        return [("token", token)]

    def _build_channel(self, target: str, *, secure: bool):
        if not secure:
            return grpc.insecure_channel(target)
        root_cert = open(self._ca_cert, "rb").read() if self._ca_cert else None
        private_key = open(self._key, "rb").read() if self._key else None
        certificate_chain = open(self._cert, "rb").read() if self._cert else None
        creds = grpc.ssl_channel_credentials(
            root_certificates=root_cert,
            private_key=private_key,
            certificate_chain=certificate_chain,
        )
        return grpc.secure_channel(target, creds)

    def _ensure_channel(self) -> None:
        if self._channel is not None:
            return
        last_exc: Exception | None = None
        for target, secure in self._targets:
            try:
                channel = self._build_channel(target, secure=secure)
                grant_rpc = channel.unary_unary(
                    "/etcdserverpb.Lease/LeaseGrant",
                    request_serializer=lambda payload: payload,
                    response_deserializer=lambda payload: payload,
                )
                keepalive_rpc = channel.stream_stream(
                    "/etcdserverpb.Lease/LeaseKeepAlive",
                    request_serializer=lambda payload: payload,
                    response_deserializer=lambda payload: payload,
                )
                revoke_rpc = channel.unary_unary(
                    "/etcdserverpb.Lease/LeaseRevoke",
                    request_serializer=lambda payload: payload,
                    response_deserializer=lambda payload: payload,
                )
                self._channel = channel
                self._channel_target = target
                self._grant_rpc = grant_rpc
                self._keepalive_rpc = keepalive_rpc
                self._revoke_rpc = revoke_rpc
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                LOGGER.debug("failed to initialize grpc etcd lease channel %s: %s", target, exc)
        raise RuntimeError(f"unable to connect grpc etcd lease client: {last_exc}")

    def grant_lease(self, ttl_seconds: int, *, lease_id: int = 0) -> int:
        self._ensure_channel()
        assert self._grant_rpc is not None
        response = self._grant_rpc(
            _encode_lease_grant_request(ttl_seconds, lease_id),
            timeout=self._timeout_s,
            metadata=self._metadata(),
        )
        granted_id, _ttl = _decode_lease_grant_response(response)
        return granted_id

    def keepalive(self, lease_id: int) -> LeaseKeepAliveResult:
        self._ensure_channel()
        assert self._keepalive_rpc is not None
        responses = self._keepalive_rpc(
            iter((_encode_lease_keepalive_request(lease_id),)),
            timeout=self._timeout_s,
            metadata=self._metadata(),
        )
        raw = next(iter(responses))
        kept_lease_id, ttl = _decode_lease_keepalive_response(raw)
        return LeaseKeepAliveResult(lease_id=kept_lease_id, ttl_seconds=ttl)

    def revoke_lease(self, lease_id: int) -> None:
        self._ensure_channel()
        assert self._revoke_rpc is not None
        response = self._revoke_rpc(
            _encode_lease_revoke_request(lease_id),
            timeout=self._timeout_s,
            metadata=self._metadata(),
        )
        _decode_lease_revoke_response(response)

    def close(self) -> None:
        if self._channel is None:
            return
        try:
            self._channel.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass
        finally:
            self._channel = None
            self._channel_target = None
            self._grant_rpc = None
            self._keepalive_rpc = None
            self._revoke_rpc = None


__all__ = [
    "GrpcEtcdLeaseClient",
    "LeaseKeepAliveResult",
]
