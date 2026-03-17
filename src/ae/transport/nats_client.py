"""Minimal NATS client wrapper for Phase 2 transport wiring."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # Optional dependency until full transport is wired.
    from nats.aio.client import Client as NATS
    from nats.aio.msg import Msg
    from nats.js.api import (
        AckPolicy,
        ConsumerConfig,
        DeliverPolicy,
        RetentionPolicy,
        StorageType,
        StreamConfig,
    )
except Exception as exc:  # pragma: no cover - exercised when dependency missing
    NATS = None  # type: ignore[assignment]
    Msg = None  # type: ignore[assignment]
    AckPolicy = None  # type: ignore[assignment]
    ConsumerConfig = None  # type: ignore[assignment]
    DeliverPolicy = None  # type: ignore[assignment]
    RetentionPolicy = None  # type: ignore[assignment]
    StorageType = None  # type: ignore[assignment]
    StreamConfig = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:  # pragma: no cover - import success
    _IMPORT_ERROR = None


class NatsClientError(RuntimeError):
    pass


@dataclass(slots=True)
class NatsMessage:
    subject: str
    reply: str | None
    data: bytes

    def json(self) -> dict:
        return json.loads(self.data.decode("utf-8"))


@dataclass(slots=True)
class JetStreamMessage:
    subject: str
    reply: str | None
    data: bytes
    stream: str | None
    consumer: str | None
    seq: int | None
    _ack: Callable[[], None]
    _ack_sync: Callable[[], None]
    _in_progress: Callable[[], None]
    _nak: Callable[[float | None], None]

    def ack(self) -> None:
        self._ack()

    def ack_sync(self) -> None:
        self._ack_sync()

    def in_progress(self) -> None:
        self._in_progress()

    def nak(self, delay_s: float | None = None) -> None:
        self._nak(delay_s)


class NatsClient:
    def __init__(
        self,
        *,
        url: str,
        creds: Path | None = None,
        name: str | None = None,
        js_domain: str | None = None,
        connect_timeout_s: float = 2.5,
    ) -> None:
        if NATS is None:  # pragma: no cover - handled via runtime logs
            raise NatsClientError(f"nats-py not available: {_IMPORT_ERROR}")
        self._url = url
        self._creds = creds
        self._name = name
        self._connect_timeout_s = connect_timeout_s
        self._js_domain = (js_domain or os.getenv("AE_JS_DOMAIN") or "").strip() or None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._nc = NATS()
        self._connected = False
        self._started = False
        self._js_subs: dict[tuple[str, str], object] = {}
        self._closing = False

    async def _on_error(self, _exc) -> None:  # noqa: ANN001
        if self._closing:
            return
        return

    async def _on_disconnect(self) -> None:
        if self._closing:
            return
        return

    async def _on_reconnect(self) -> None:
        if self._closing:
            return
        return

    async def _on_closed(self) -> None:
        return

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect(self) -> None:
        if self._connected:
            return
        if not self._started:
            self._thread.start()
            self._started = True
        options = {
            "servers": [self._url],
            "name": self._name,
            "connect_timeout": self._connect_timeout_s,
            "error_cb": self._on_error,
            "disconnected_cb": self._on_disconnect,
            "reconnected_cb": self._on_reconnect,
            "closed_cb": self._on_closed,
        }
        if self._creds:
            options["user_credentials"] = str(self._creds)
        fut = asyncio.run_coroutine_threadsafe(self._nc.connect(**options), self._loop)
        try:
            fut.result(timeout=self._connect_timeout_s + 1)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"connect failed: {exc}") from exc
        self._connected = True

    def close(self, timeout_s: float = 5.0) -> None:
        self._closing = True
        if not self._started:
            return
        if self._loop.is_running():
            try:
                async def _shutdown():  # type: ignore[no-untyped-def]
                    if self._connected:
                        try:
                            await self._nc.close()
                        except Exception:
                            pass
                    await asyncio.sleep(0)
                    try:
                        tasks = [
                            task
                            for task in asyncio.all_tasks()
                            if task is not asyncio.current_task()
                        ]
                    except Exception:
                        tasks = []
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        asyncio.get_running_loop().stop()
                    except Exception:
                        pass

                fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
                fut.result(timeout=timeout_s)
            except Exception:
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except Exception:
                    pass
        try:
            self._thread.join(timeout=timeout_s)
        except Exception:
            pass
        try:
            if not self._loop.is_running():
                self._loop.close()
        except Exception:
            pass
        self._connected = False

    def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        self._ensure_connected()
        coro = self._nc.publish(subject, payload, headers=headers)
        if threading.current_thread() is self._thread:
            try:
                self._loop.create_task(coro)
                return
            except Exception as exc:  # noqa: BLE001
                raise NatsClientError(f"publish failed: {exc}") from exc
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"publish failed: {exc}") from exc

    def publish_json(
        self,
        subject: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.publish(subject, body, headers=headers, timeout_s=timeout_s)

    def publish_js(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 2.5,
    ) -> None:
        self._ensure_connected()

        async def _pub():  # type: ignore[no-untyped-def]
            js = self._js()
            await js.publish(subject, payload, headers=headers)

        try:
            self._run(_pub(), timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"js publish failed: {exc}") from exc

    def publish_js_json(
        self,
        subject: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 2.5,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.publish_js(subject, body, headers=headers, timeout_s=timeout_s)

    def request(
        self, subject: str, payload: bytes, *, timeout_s: float = 2.0
    ) -> NatsMessage:
        self._ensure_connected()
        fut = asyncio.run_coroutine_threadsafe(
            self._nc.request(subject, payload, timeout=timeout_s), self._loop
        )
        try:
            msg = fut.result(timeout=timeout_s + 1)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"request failed: {exc}") from exc
        return NatsMessage(subject=msg.subject, reply=msg.reply, data=msg.data)

    def request_json(
        self, subject: str, payload: dict, *, timeout_s: float = 2.0
    ) -> dict:
        body = json.dumps(payload).encode("utf-8")
        msg = self.request(subject, body, timeout_s=timeout_s)
        return msg.json()

    def subscribe(
        self,
        subject: str,
        cb: Callable[[NatsMessage], None],
    ) -> str:
        self._ensure_connected()

        async def _handler(msg: Msg) -> None:  # type: ignore[misc]
            try:
                cb(NatsMessage(subject=msg.subject, reply=msg.reply, data=msg.data))
            except Exception:
                pass

        fut = asyncio.run_coroutine_threadsafe(
            self._nc.subscribe(subject, cb=_handler), self._loop
        )
        try:
            sid = fut.result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"subscribe failed: {exc}") from exc
        return str(sid)

    def unsubscribe(self, sid: str) -> None:
        self._ensure_connected()
        fut = asyncio.run_coroutine_threadsafe(self._nc.unsubscribe(int(sid)), self._loop)
        try:
            fut.result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"unsubscribe failed: {exc}") from exc

    def fetch_js_messages(
        self,
        *,
        subject: str,
        durable: str,
        batch: int,
        timeout_s: float,
        stream: str | None = None,
    ) -> list[JetStreamMessage]:
        self._ensure_connected()

        async def _get_pull_sub():  # type: ignore[no-untyped-def]
            key = (stream or subject, durable)
            sub = self._js_subs.get(key)
            if sub is None:
                js = self._js()
                if stream:
                    sub = await js.pull_subscribe_bind(stream=stream, durable=durable)
                else:
                    sub = await js.pull_subscribe(subject, durable=durable)
                self._js_subs[key] = sub
            return sub

        async def _fetch():  # type: ignore[no-untyped-def]
            sub = await _get_pull_sub()
            return await sub.fetch(batch, timeout=timeout_s)

        try:
            msgs = self._run(_fetch(), timeout_s + 1.0)
        except Exception:
            return []
        return [self._wrap_js_msg(msg) for msg in msgs]

    def ensure_stream(
        self,
        *,
        name: str,
        subjects: list[str],
        storage: str = "file",
        retention: str = "workqueue",
        replicas: int = 1,
    ) -> None:
        self._ensure_connected()
        if StreamConfig is None:
            raise NatsClientError("jetstream api unavailable")

        async def _ensure():  # type: ignore[no-untyped-def]
            js = self._js()
            cfg = StreamConfig(
                name=name,
                subjects=subjects,
                retention=_retention_policy(retention),
                storage=_storage_type(storage),
                num_replicas=max(1, int(replicas or 1)),
            )
            try:
                await js.stream_info(name)
                try:
                    await js.update_stream(cfg)
                    return
                except Exception:
                    await js.stream_info(name)
                    return
            except Exception:
                pass
            await js.add_stream(cfg)

        self._run(_ensure(), 5.0)

    def ensure_consumer(
        self,
        *,
        stream: str,
        durable: str,
        filter_subject: str,
        ack_wait_s: float,
        max_ack_pending: int,
        max_deliver: int,
        max_waiting: int,
        replicas: int = 1,
    ) -> None:
        self._ensure_connected()
        if ConsumerConfig is None:
            raise NatsClientError("jetstream api unavailable")

        async def _ensure():  # type: ignore[no-untyped-def]
            js = self._js()
            cfg = ConsumerConfig(
                durable_name=durable,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=_ack_wait_value(ack_wait_s),
                max_ack_pending=max_ack_pending,
                max_deliver=max_deliver,
                max_waiting=max_waiting,
                filter_subject=filter_subject,
                deliver_policy=DeliverPolicy.ALL,
                num_replicas=max(1, int(replicas or 1)),
            )
            try:
                await js.consumer_info(stream, durable)
                try:
                    await js.update_consumer(stream, cfg)
                    return
                except Exception:
                    await js.consumer_info(stream, durable)
                    return
            except Exception:
                pass
            await js.add_consumer(stream, cfg)

        self._run(_ensure(), 5.0)

    def stream_info(self, name: str):
        self._ensure_connected()

        async def _info():  # type: ignore[no-untyped-def]
            js = self._js()
            return await js.stream_info(name)

        try:
            return self._run(_info(), 2.5)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"js stream info failed: {exc}") from exc

    def consumer_info(self, stream: str, durable: str):
        self._ensure_connected()

        async def _info():  # type: ignore[no-untyped-def]
            js = self._js()
            return await js.consumer_info(stream, durable)

        try:
            return self._run(_info(), 2.5)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"js consumer info failed: {exc}") from exc

    def validate_stream(
        self,
        *,
        name: str,
        subjects: list[str],
        storage: str,
        retention: str,
        replicas: int,
    ) -> None:
        info = self.stream_info(name)
        drift = _stream_config_drift(
            info,
            subjects=subjects,
            storage=storage,
            retention=retention,
            replicas=replicas,
        )
        if drift:
            raise NatsClientError(f"js stream {name} drift: {', '.join(drift)}")

    def validate_consumer(
        self,
        *,
        stream: str,
        durable: str,
        filter_subject: str,
        ack_wait_s: float,
        max_ack_pending: int,
        max_deliver: int,
        max_waiting: int,
        replicas: int,
    ) -> None:
        info = self.consumer_info(stream, durable)
        drift = _consumer_config_drift(
            info,
            filter_subject=filter_subject,
            ack_wait_s=ack_wait_s,
            max_ack_pending=max_ack_pending,
            max_deliver=max_deliver,
            max_waiting=max_waiting,
            replicas=replicas,
        )
        if drift:
            raise NatsClientError(f"js consumer {durable} drift: {', '.join(drift)}")

    def _wrap_js_msg(self, msg: Msg) -> JetStreamMessage:  # type: ignore[misc]
        def _ack() -> None:
            try:
                self._run(msg.ack(), 2.0)
            except Exception:
                pass

        def _ack_sync() -> None:
            try:
                if hasattr(msg, "ack_sync"):
                    self._run(msg.ack_sync(), 2.0)  # type: ignore[attr-defined]
                else:
                    self._run(msg.ack(), 2.0)
            except Exception:
                pass

        def _in_progress() -> None:
            try:
                if hasattr(msg, "in_progress"):
                    self._run(msg.in_progress(), 2.0)  # type: ignore[attr-defined]
            except Exception:
                pass

        def _nak(delay_s: float | None) -> None:
            try:
                if hasattr(msg, "nak"):
                    if delay_s is None:
                        self._run(msg.nak(), 2.0)  # type: ignore[attr-defined]
                    else:
                        self._run(msg.nak(delay=delay_s), 2.0)  # type: ignore[attr-defined]
            except Exception:
                pass

        stream = None
        consumer = None
        seq = None
        meta = getattr(msg, "metadata", None)
        if meta is not None:
            stream = getattr(meta, "stream", None) or getattr(meta, "stream_name", None)
            consumer = getattr(meta, "consumer", None) or getattr(meta, "consumer_name", None)
            seq_meta = getattr(meta, "sequence", None)
            if isinstance(seq_meta, int):
                seq = seq_meta
            elif seq_meta is not None:
                seq = getattr(seq_meta, "stream", None) or getattr(seq_meta, "consumer", None)

        return JetStreamMessage(
            subject=msg.subject,
            reply=msg.reply,
            data=msg.data,
            stream=stream,
            consumer=consumer,
            seq=int(seq) if seq is not None else None,
            _ack=_ack,
            _ack_sync=_ack_sync,
            _in_progress=_in_progress,
            _nak=_nak,
        )

    def _run(self, coro, timeout_s: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout_s)

    def _js(self):  # type: ignore[no-untyped-def]
        if self._js_domain:
            return self._nc.jetstream(domain=self._js_domain)
        return self._nc.jetstream()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise NatsClientError("client not connected")


def connect_once(
    url: str,
    *,
    creds: Path | None = None,
    name: str | None = None,
    timeout_s: float = 2.5,
) -> None:
    client = NatsClient(url=url, creds=creds, name=name, connect_timeout_s=timeout_s)
    client.connect()
    client.close(timeout_s=timeout_s)


def _storage_type(value: str):
    if StorageType is None:
        return None
    raw = str(value or "").lower()
    if raw in {"mem", "memory"}:
        return StorageType.MEMORY
    return StorageType.FILE


def _retention_policy(value: str):
    if RetentionPolicy is None:
        return None
    raw = str(value or "").lower()
    if raw in {"workqueue", "work_queue", "queue"}:
        return RetentionPolicy.WORK_QUEUE
    if raw in {"interest"}:
        return RetentionPolicy.INTEREST
    return RetentionPolicy.LIMITS


def _read(obj, *names: str, default=None):  # type: ignore[no-untyped-def]
    for name in names:
        try:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            continue
    return default


def _enum_text(value) -> str:  # type: ignore[no-untyped-def]
    if value is None:
        return ""
    try:
        if hasattr(value, "name"):
            return str(value.name).lower()
    except Exception:
        pass
    try:
        raw = getattr(value, "value")
        if raw is not None:
            return str(raw).split(".")[-1].lower()
    except Exception:
        pass
    return str(value).split(".")[-1].lower()


def _normalize_token(value: str) -> str:
    return str(value).strip().lower().replace("-", "").replace("_", "")


def _seconds_value(value) -> float | None:  # type: ignore[no-untyped-def]
    if value is None:
        return None
    if isinstance(value, timedelta):
        return float(value.total_seconds())
    try:
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
    except Exception:
        pass
    try:
        raw = float(value)
    except Exception:
        return None
    if raw > 1_000_000:
        return raw / 1_000_000_000.0
    return raw


def _stream_config_drift(
    info,
    *,
    subjects: list[str],
    storage: str,
    retention: str,
    replicas: int,
) -> list[str]:
    config = _read(info, "config")
    if config is None:
        return ["missing_config"]
    drift: list[str] = []
    actual_subjects = sorted(str(v) for v in (_read(config, "subjects", default=[]) or []))
    if sorted(str(v) for v in subjects) != actual_subjects:
        drift.append("subjects")
    if _normalize_token(_enum_text(_read(config, "storage"))) != _normalize_token(storage):
        drift.append("storage")
    if _normalize_token(_enum_text(_read(config, "retention"))) != _normalize_token(retention):
        drift.append("retention")
    actual_replicas = int(_read(config, "num_replicas", default=1) or 1)
    if actual_replicas != max(1, int(replicas or 1)):
        drift.append("replicas")
    return drift


def _consumer_config_drift(
    info,
    *,
    filter_subject: str,
    ack_wait_s: float,
    max_ack_pending: int,
    max_deliver: int,
    max_waiting: int,
    replicas: int,
) -> list[str]:
    config = _read(info, "config")
    if config is None:
        return ["missing_config"]
    drift: list[str] = []
    if str(_read(config, "filter_subject", default="") or "") != filter_subject:
        drift.append("filter_subject")
    actual_ack_wait = _seconds_value(_read(config, "ack_wait"))
    if actual_ack_wait is None or abs(float(actual_ack_wait) - float(ack_wait_s)) > 0.25:
        drift.append("ack_wait")
    if _enum_text(_read(config, "ack_policy")) != "explicit":
        drift.append("ack_policy")
    if _enum_text(_read(config, "deliver_policy")) != "all":
        drift.append("deliver_policy")
    if int(_read(config, "max_ack_pending", default=0) or 0) != int(max_ack_pending):
        drift.append("max_ack_pending")
    if int(_read(config, "max_deliver", default=0) or 0) != int(max_deliver):
        drift.append("max_deliver")
    if int(_read(config, "max_waiting", default=0) or 0) != int(max_waiting):
        drift.append("max_waiting")
    actual_replicas = int(_read(config, "num_replicas", default=1) or 1)
    if actual_replicas != max(1, int(replicas or 1)):
        drift.append("replicas")
    return drift


def _ack_wait_value(seconds: float):
    ann = getattr(ConsumerConfig, "__annotations__", {}) if ConsumerConfig else {}
    raw = ann.get("ack_wait")
    if raw is not None and "timedelta" in str(raw).lower():
        return timedelta(seconds=seconds)
    return float(seconds)


__all__ = [
    "JetStreamMessage",
    "NatsClient",
    "NatsClientError",
    "NatsMessage",
    "_consumer_config_drift",
    "_stream_config_drift",
    "connect_once",
]
