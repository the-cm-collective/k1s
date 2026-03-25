from __future__ import annotations

import base64
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ae import __version__ as AE_VERSION
from ae.controller.etcd_lease_client import GrpcEtcdLeaseClient


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _b64encode(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.b64encode(raw).decode("ascii")


class KvClient(Protocol):
    def put(self, key: str, value: str, *, lease: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def range(
        self,
        key: str,
        *,
        range_end: str | bytes | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]: ...

    def txn(self, compare: list[dict], success: list[dict], failure: list[dict]) -> dict: ...


class LeaseClient(Protocol):
    def grant_lease(self, ttl_seconds: int, *, lease_id: int = 0) -> int: ...

    def keepalive(self, lease_id: int): ...

    def revoke_lease(self, lease_id: int) -> None: ...

    def close(self) -> None: ...


def _prefix_end(prefix: str | bytes) -> bytes:
    raw = prefix.encode("utf-8") if isinstance(prefix, str) else prefix
    if not raw:
        return b"\x00"
    buf = bytearray(raw)
    for idx in range(len(buf) - 1, -1, -1):
        if buf[idx] < 0xFF:
            buf[idx] += 1
            return bytes(buf[: idx + 1])
    return b"\x00"


@dataclass(slots=True, frozen=True)
class AuthorityConfig:
    enabled: bool
    controller_id: str
    advertise_addr: str | None
    etcd_prefix: str
    lease_ttl_seconds: int = 15
    keepalive_interval_seconds: float = 5.0
    standby_retry_min_seconds: float = 1.0
    standby_retry_max_seconds: float = 2.0
    follower_poll_seconds: float = 2.0
    version: str = "v1"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AuthorityConfig":
        env_map = env or os.environ
        enabled = _truthy(env_map.get("AE_HA_MODE"))
        controller_id = (env_map.get("AE_CONTROLLER_ID") or "").strip()
        if not controller_id:
            controller_id = os.uname().nodename
        advertise_addr = (env_map.get("AE_CONTROLLER_ADVERTISE_ADDR") or "").strip() or None
        prefix = (env_map.get("AE_ETCD_PREFIX") or "k1s/v1").strip("/")
        return cls(
            enabled=enabled,
            controller_id=controller_id,
            advertise_addr=advertise_addr,
            etcd_prefix=prefix,
            lease_ttl_seconds=max(
                5,
                int(env_map.get("AE_CONTROLPLANE_LEASE_TTL_SECONDS", "15") or 15),
            ),
            keepalive_interval_seconds=max(
                1.0,
                float(env_map.get("AE_CONTROLPLANE_KEEPALIVE_SECONDS", "5") or 5),
            ),
            standby_retry_min_seconds=max(
                0.2,
                float(env_map.get("AE_CONTROLPLANE_STANDBY_RETRY_MIN_SECONDS", "1") or 1),
            ),
            standby_retry_max_seconds=max(
                0.2,
                float(env_map.get("AE_CONTROLPLANE_STANDBY_RETRY_MAX_SECONDS", "2") or 2),
            ),
            follower_poll_seconds=max(
                0.2,
                float(env_map.get("AE_CONTROLPLANE_FOLLOWER_POLL_SECONDS", "2") or 2),
            ),
            version=str(env_map.get("AE_CONTROLPLANE_AUTHORITY_VERSION") or "v1"),
        )

    @property
    def leader_key(self) -> str:
        return "/".join(part for part in (self.etcd_prefix, "controlplane", "leader") if part)

    @property
    def controllers_prefix(self) -> str:
        return "/".join(part for part in (self.etcd_prefix, "controlplane", "controllers") if part)

    @property
    def presence_key(self) -> str:
        return "/".join((self.controllers_prefix, self.controller_id))

    @property
    def presence_stale_after_seconds(self) -> float:
        return float(
            min(
                int(self.lease_ttl_seconds),
                max(1.0, float(self.keepalive_interval_seconds) * 2.0),
            )
        )


@dataclass(slots=True, frozen=True)
class LeaderInfo:
    controller_id: str
    controller_epoch: int
    lease_id: int
    advertise_addr: str | None
    acquired_at: datetime | None
    version: str


@dataclass(slots=True, frozen=True)
class AuthorityMember:
    controller_id: str
    advertise_addr: str | None
    version: str
    heartbeat_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class AuthoritySnapshot:
    enabled: bool
    controller_id: str
    is_leader: bool
    leader_info: LeaderInfo | None

    @property
    def controller_epoch(self) -> int:
        if self.leader_info is None:
            return 0
        return int(self.leader_info.controller_epoch)


class NotLeaderError(RuntimeError):
    """Raised when a follower receives a leader-only mutation."""

    def __init__(self, leader_info: LeaderInfo | None) -> None:
        self.leader_info = leader_info
        message = "not_leader"
        if leader_info is not None:
            details = [f"controller_id={leader_info.controller_id}"]
            if leader_info.advertise_addr:
                details.append(f"advertise_addr={leader_info.advertise_addr}")
            if leader_info.controller_epoch:
                details.append(f"controller_epoch={leader_info.controller_epoch}")
            message = f"{message}: {' '.join(details)}"
        super().__init__(message)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": "not_leader"}
        if self.leader_info is not None:
            payload["controller_id"] = self.leader_info.controller_id
            payload["controller_epoch"] = self.leader_info.controller_epoch
            if self.leader_info.advertise_addr:
                payload["advertise_addr"] = self.leader_info.advertise_addr
        return payload


@dataclass(slots=True)
class _LeaseState:
    lease_id: int
    renew_at: float


@dataclass(slots=True)
class ControllerAuthorityService:
    config: AuthorityConfig
    kv_client: KvClient
    lease_client: LeaseClient
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _is_leader: bool = field(default=False, init=False)
    _leader_info: LeaderInfo | None = field(default=None, init=False)
    _presence_lease: _LeaseState | None = field(default=None, init=False)
    _leader_lease: _LeaseState | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _leader_lost: threading.Event = field(default_factory=threading.Event, init=False)
    _next_election_attempt_at: float = field(default=0.0, init=False)
    _next_leader_poll_at: float = field(default=0.0, init=False)
    _presence_observed_at: str | None = field(default=None, init=False)

    @classmethod
    def from_env(
        cls,
        *,
        env: dict[str, str] | None = None,
        kv_client: KvClient | None = None,
        lease_client: LeaseClient | None = None,
    ) -> "ControllerAuthorityService":
        config = AuthorityConfig.from_env(env)
        if kv_client is None:
            from ae.controller.etcd_state import EtcdHttpClient

            env_map = env or os.environ
            raw_endpoints = [
                e.strip()
                for e in (env_map.get("AE_ETCD_ENDPOINTS") or "http://127.0.0.1:2379").split(",")
            ]
            kv_client = EtcdHttpClient(
                raw_endpoints,
                api_prefix=env_map.get("AE_ETCD_API_PREFIX") or None,
                timeout_s=float(env_map.get("AE_ETCD_OP_TIMEOUT", "3") or 3),
                ca_cert=env_map.get("AE_ETCD_CA") or None,
                cert=env_map.get("AE_ETCD_CERT") or None,
                key=env_map.get("AE_ETCD_KEY") or None,
                user=env_map.get("AE_ETCD_USER") or None,
                password=env_map.get("AE_ETCD_PASSWORD") or None,
            )
        if lease_client is None:
            token_provider = getattr(kv_client, "_token", None)
            if callable(token_provider):
                token_fn = token_provider
            else:
                token_fn = lambda: getattr(kv_client, "_token", None)
            lease_client = GrpcEtcdLeaseClient.from_env(
                env=env,
                token_provider=token_fn,
            )
        return cls(config=config, kv_client=kv_client, lease_client=lease_client)

    @property
    def ready(self) -> threading.Event:
        return self._ready

    @property
    def leader_lost(self) -> threading.Event:
        return self._leader_lost

    def snapshot(self) -> AuthoritySnapshot:
        with self._lock:
            return AuthoritySnapshot(
                enabled=self.config.enabled,
                controller_id=self.config.controller_id,
                is_leader=self._is_leader or not self.config.enabled,
                leader_info=self._leader_info,
            )

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.config.enabled:
            with self._lock:
                self._is_leader = True
                self._leader_info = LeaderInfo(
                    controller_id=self.config.controller_id,
                    controller_epoch=int(os.getenv("AE_CONTROLLER_EPOCH", "0") or 0),
                    lease_id=0,
                    advertise_addr=self.config.advertise_addr,
                    acquired_at=None,
                    version=self.config.version,
                )
            self._ready.set()
            return
        self._thread = threading.Thread(
            name="controller-authority",
            target=self._run,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.config.enabled:
            self._cleanup()
        try:
            self.lease_client.close()
        except Exception:
            pass

    def run_once(self, *, now_monotonic: float | None = None) -> float:
        if not self.config.enabled:
            with self._lock:
                self._is_leader = True
            self._ready.set()
            return 1.0
        now = time.monotonic() if now_monotonic is None else now_monotonic
        self._ensure_presence(now)
        if self._is_leader:
            self._renew_leader(now)
        else:
            if now >= self._next_leader_poll_at:
                self._leader_info = self._read_leader_info()
                self._ready.set()
                self._next_leader_poll_at = now + self.config.follower_poll_seconds
            if self._leader_info is None and now >= self._next_election_attempt_at:
                self._attempt_leadership(now)
        return self._next_sleep(now)

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def list_members(self) -> list[AuthorityMember]:
        if not self.config.enabled:
            return []
        try:
            response = self.kv_client.range(
                self.config.controllers_prefix,
                range_end=_prefix_end(self.config.controllers_prefix),
            )
        except Exception:
            return []
        members: list[AuthorityMember] = []
        for kv in list(response.get("kvs") or []):
            try:
                key = base64.b64decode(str(kv.get("key", "")).encode("ascii")).decode("utf-8")
            except Exception:
                key = ""
            try:
                raw = base64.b64decode(str(kv.get("value", "")).encode("ascii")).decode("utf-8")
                rec = json.loads(raw)
            except Exception:
                continue
            key_controller_id = key.rsplit("/", 1)[-1] if key else ""
            controller_id = str(rec.get("controller_id") or key_controller_id or "").strip()
            if not controller_id:
                continue
            members.append(
                AuthorityMember(
                    controller_id=controller_id,
                    advertise_addr=str(rec.get("advertise_addr") or "").strip() or None,
                    version=str(rec.get("version") or AE_VERSION),
                    heartbeat_at=_dt_from_iso(rec.get("heartbeat_at")),
                )
            )
        members.sort(key=lambda member: member.controller_id)
        return members

    def _run(self) -> None:
        delay = 0.1
        while not self._stop.wait(delay):
            try:
                delay = self.run_once()
            except Exception:
                delay = min(1.0, self.config.follower_poll_seconds)
                self._ready.set()

    def _cleanup(self) -> None:
        leader_lease_id = self._leader_lease.lease_id if self._leader_lease is not None else None
        presence_lease_id = (
            self._presence_lease.lease_id if self._presence_lease is not None else None
        )
        if leader_lease_id:
            try:
                self.lease_client.revoke_lease(leader_lease_id)
            except Exception:
                pass
        if presence_lease_id and presence_lease_id != leader_lease_id:
            try:
                self.lease_client.revoke_lease(presence_lease_id)
            except Exception:
                pass
        try:
            self.kv_client.delete(self.config.presence_key)
        except Exception:
            pass
        with self._lock:
            self._presence_lease = None
            self._leader_lease = None
            self._is_leader = False
            self._leader_info = None
            self._presence_observed_at = None

    def _publish_presence(self, lease_id: int, *, heartbeat_at: str | None = None) -> str:
        heartbeat_iso = heartbeat_at or _now_iso()
        observed_at = self._presence_observed_at or heartbeat_iso
        self.kv_client.put(
            self.config.presence_key,
            self._encode_json(
                {
                    "controller_id": self.config.controller_id,
                    "advertise_addr": self.config.advertise_addr,
                    "observed_at": observed_at,
                    "heartbeat_at": heartbeat_iso,
                    "version": AE_VERSION,
                }
            ),
            lease=lease_id,
        )
        with self._lock:
            self._presence_observed_at = observed_at
        return heartbeat_iso

    def _ensure_presence(self, now: float) -> None:
        if self._presence_lease is None:
            lease_id = self.lease_client.grant_lease(self.config.lease_ttl_seconds)
            self._publish_presence(lease_id)
            with self._lock:
                self._presence_lease = _LeaseState(
                    lease_id=lease_id,
                    renew_at=now + self.config.keepalive_interval_seconds,
                )
            self._ready.set()
            return
        if now < self._presence_lease.renew_at:
            return
        try:
            self.lease_client.keepalive(self._presence_lease.lease_id)
        except Exception:
            lease_id = self.lease_client.grant_lease(self.config.lease_ttl_seconds)
            self._publish_presence(lease_id)
            with self._lock:
                self._presence_lease = _LeaseState(
                    lease_id=lease_id,
                    renew_at=now + self.config.keepalive_interval_seconds,
                )
            return
        self._publish_presence(self._presence_lease.lease_id)
        with self._lock:
            assert self._presence_lease is not None
            self._presence_lease = _LeaseState(
                lease_id=self._presence_lease.lease_id,
                renew_at=now + self.config.keepalive_interval_seconds,
            )

    def _attempt_leadership(self, now: float) -> None:
        lease_id = self.lease_client.grant_lease(self.config.lease_ttl_seconds)
        leader_record = {
            "controller_id": self.config.controller_id,
            "lease_id": lease_id,
            "acquired_at": _now_iso(),
            "advertise_addr": self.config.advertise_addr,
            "version": self.config.version,
        }
        compare = [
            {
                "key": _b64encode(self.config.leader_key),
                "target": "CREATE",
                "createRevision": "0",
            }
        ]
        success = [
            {
                "requestPut": {
                    "key": _b64encode(self.config.leader_key),
                    "value": _b64encode(self._encode_json(leader_record)),
                    "lease": lease_id,
                }
            }
        ]
        failure = [{"requestRange": {"key": _b64encode(self.config.leader_key), "limit": 1}}]
        claimed = False
        try:
            resp = self.kv_client.txn(compare, success, failure)
            claimed = bool(resp.get("succeeded"))
        except Exception:
            try:
                self.lease_client.revoke_lease(lease_id)
            except Exception:
                pass
            self._schedule_retry(now)
            raise
        if not claimed:
            try:
                self.lease_client.revoke_lease(lease_id)
            except Exception:
                pass
            with self._lock:
                self._leader_info = self._read_leader_info()
            self._schedule_retry(now)
            self._ready.set()
            return
        leader_info = self._read_leader_info()
        if leader_info is None:
            try:
                self.lease_client.revoke_lease(lease_id)
            except Exception:
                pass
            self._schedule_retry(now)
            return
        with self._lock:
            self._is_leader = leader_info.controller_id == self.config.controller_id
            self._leader_info = leader_info
            if self._is_leader:
                self._leader_lease = _LeaseState(
                    lease_id=lease_id,
                    renew_at=now + self.config.keepalive_interval_seconds,
                )
                self._leader_lost.clear()
        self._ready.set()

    def _renew_leader(self, now: float) -> None:
        if self._leader_lease is None:
            with self._lock:
                self._is_leader = False
            self._schedule_retry(now)
            return
        if now < self._leader_lease.renew_at:
            return
        try:
            self.lease_client.keepalive(self._leader_lease.lease_id)
        except Exception:
            self._lose_leadership(now)
            return
        leader_info = self._read_leader_info()
        if leader_info is None or leader_info.controller_id != self.config.controller_id:
            self._lose_leadership(now)
            return
        with self._lock:
            self._leader_info = leader_info
            self._leader_lease = _LeaseState(
                lease_id=self._leader_lease.lease_id,
                renew_at=now + self.config.keepalive_interval_seconds,
            )

    def _lose_leadership(self, now: float) -> None:
        with self._lock:
            self._is_leader = False
            self._leader_lease = None
            self._leader_info = self._read_leader_info()
            self._leader_lost.set()
        self._schedule_retry(now)

    def _schedule_retry(self, now: float) -> None:
        low = self.config.standby_retry_min_seconds
        high = max(low, self.config.standby_retry_max_seconds)
        self._next_election_attempt_at = now + random.uniform(low, high)
        self._next_leader_poll_at = now + self.config.follower_poll_seconds

    def _next_sleep(self, now: float) -> float:
        targets = [now + self.config.follower_poll_seconds]
        if self._presence_lease is not None:
            targets.append(self._presence_lease.renew_at)
        if self._leader_lease is not None:
            targets.append(self._leader_lease.renew_at)
        if not self._is_leader:
            targets.append(
                self._next_election_attempt_at or (now + self.config.standby_retry_min_seconds)
            )
            targets.append(self._next_leader_poll_at or (now + self.config.follower_poll_seconds))
        next_at = min(targets)
        return max(0.05, min(1.0, next_at - now))

    def _read_leader_info(self) -> LeaderInfo | None:
        response = self.kv_client.range(self.config.leader_key, limit=1)
        kvs = response.get("kvs") or []
        if not kvs:
            return None
        kv = kvs[0]
        raw = base64.b64decode(str(kv.get("value", "")).encode("ascii")).decode("utf-8")
        rec = json.loads(raw)
        return LeaderInfo(
            controller_id=str(rec.get("controller_id", "")),
            controller_epoch=int(kv.get("mod_revision") or 0),
            lease_id=int(rec.get("lease_id") or 0),
            advertise_addr=rec.get("advertise_addr") or None,
            acquired_at=_dt_from_iso(rec.get("acquired_at")),
            version=str(rec.get("version") or self.config.version),
        )

    @staticmethod
    def _encode_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AuthorityConfig",
    "AuthorityMember",
    "AuthoritySnapshot",
    "ControllerAuthorityService",
    "LeaderInfo",
    "NotLeaderError",
]
