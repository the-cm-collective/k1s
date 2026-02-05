"""Transport feature flags and NATS/gateway configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRANSPORT_BACKEND = "http"
DEFAULT_GATEWAY_ACK_WAIT = "30s"
DEFAULT_GATEWAY_PROGRESS_INTERVAL = "10s"
DEFAULT_GATEWAY_PROGRESS_JITTER_PCT = 15
DEFAULT_GATEWAY_MAX_ACK_PENDING = 32
DEFAULT_GATEWAY_MAX_DELIVER = 20
DEFAULT_GATEWAY_MAX_WAITING = 512
DEFAULT_GATEWAY_SPOOL_PATH = Path("/var/lib/ae/gateway/spool.db")


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class TransportConfig:
    """Top-level transport feature flags."""

    backend: str
    nats_url: str | None
    nats_creds: Path | None
    site_id: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TransportConfig:
        use_env = env if env is not None else os.environ
        backend = (use_env.get("AE_TRANSPORT_BACKEND") or DEFAULT_TRANSPORT_BACKEND).strip()
        nats_url = use_env.get("AE_NATS_URL") or None
        creds_raw = use_env.get("AE_NATS_CREDS")
        nats_creds = Path(creds_raw) if creds_raw else None
        site_id = use_env.get("AE_SITE_ID") or None
        return cls(
            backend=backend.lower(),
            nats_url=nats_url,
            nats_creds=nats_creds,
            site_id=site_id,
        )


@dataclass(slots=True)
class GatewayJetStreamConfig:
    """Site Gateway JetStream pull/ack settings (Option A defaults)."""

    ack_wait: str
    progress_interval: str
    progress_jitter_pct: int
    max_ack_pending: int
    max_deliver: int
    max_waiting: int
    spool_path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayJetStreamConfig:
        use_env = env if env is not None else os.environ
        ack_wait = use_env.get("AE_GATEWAY_JS_ACK_WAIT") or DEFAULT_GATEWAY_ACK_WAIT
        progress_interval = (
            use_env.get("AE_GATEWAY_JS_ACK_PROGRESS_INTERVAL")
            or DEFAULT_GATEWAY_PROGRESS_INTERVAL
        )
        progress_jitter_pct = _env_int(
            use_env,
            "AE_GATEWAY_JS_ACK_PROGRESS_JITTER_PCT",
            DEFAULT_GATEWAY_PROGRESS_JITTER_PCT,
        )
        max_ack_pending = _env_int(
            use_env, "AE_GATEWAY_JS_MAX_ACK_PENDING", DEFAULT_GATEWAY_MAX_ACK_PENDING
        )
        max_deliver = _env_int(use_env, "AE_GATEWAY_JS_MAX_DELIVER", DEFAULT_GATEWAY_MAX_DELIVER)
        max_waiting = _env_int(use_env, "AE_GATEWAY_JS_MAX_WAITING", DEFAULT_GATEWAY_MAX_WAITING)
        spool_raw = use_env.get("AE_GATEWAY_SPOOL_PATH")
        spool_path = Path(spool_raw) if spool_raw else DEFAULT_GATEWAY_SPOOL_PATH
        return cls(
            ack_wait=ack_wait,
            progress_interval=progress_interval,
            progress_jitter_pct=progress_jitter_pct,
            max_ack_pending=max_ack_pending,
            max_deliver=max_deliver,
            max_waiting=max_waiting,
            spool_path=spool_path,
        )
