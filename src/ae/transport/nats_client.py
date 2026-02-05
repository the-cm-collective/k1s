"""Minimal NATS client wrapper for Phase 2 transport wiring."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:  # Optional dependency until full transport is wired.
    from nats.aio.client import Client as NATS
    from nats.aio.msg import Msg
except Exception as exc:  # pragma: no cover - exercised when dependency missing
    NATS = None  # type: ignore[assignment]
    Msg = None  # type: ignore[assignment]
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


class NatsClient:
    def __init__(
        self,
        *,
        url: str,
        creds: Path | None = None,
        name: str | None = None,
        connect_timeout_s: float = 2.5,
    ) -> None:
        if NATS is None:  # pragma: no cover - handled via runtime logs
            raise NatsClientError(f"nats-py not available: {_IMPORT_ERROR}")
        self._url = url
        self._creds = creds
        self._name = name
        self._connect_timeout_s = connect_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._nc = NATS()
        self._connected = False
        self._started = False

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
        }
        if self._creds:
            options["user_credentials"] = str(self._creds)
        fut = asyncio.run_coroutine_threadsafe(self._nc.connect(**options), self._loop)
        try:
            fut.result(timeout=self._connect_timeout_s + 1)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"connect failed: {exc}") from exc
        self._connected = True

    def close(self, timeout_s: float = 2.5) -> None:
        if not self._connected:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._nc.drain(), self._loop)
            fut.result(timeout=timeout_s)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        try:
            self._thread.join(timeout=timeout_s)
        except Exception:
            pass
        self._connected = False

    def publish(self, subject: str, payload: bytes, *, timeout_s: float = 2.0) -> None:
        self._ensure_connected()
        fut = asyncio.run_coroutine_threadsafe(
            self._nc.publish(subject, payload), self._loop
        )
        try:
            fut.result(timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise NatsClientError(f"publish failed: {exc}") from exc

    def publish_json(self, subject: str, payload: dict, *, timeout_s: float = 2.0) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.publish(subject, body, timeout_s=timeout_s)

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


__all__ = ["NatsClient", "NatsClientError", "NatsMessage", "connect_once"]
