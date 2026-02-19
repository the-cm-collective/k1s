from __future__ import annotations

from typing import Any

import pytest
import requests

from ae.controller.etcd_state import EtcdHttpClient


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")


def test_post_fallbacks_to_v3alpha_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    responses = [
        _Resp(404, text="not found"),
        _Resp(200, {"ok": True}),
    ]

    def _fake_post(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr("ae.controller.etcd_state.requests.post", _fake_post)
    client = EtcdHttpClient(["http://127.0.0.1:2379"])
    out = client._post("/kv/put", {"key": "a", "value": "b"})

    assert out == {"ok": True}
    assert any("/v3/kv/put" in u for u in calls)
    assert any("/v3alpha/kv/put" in u for u in calls)


def test_post_retries_429_then_succeeds_without_v3alpha(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    responses = [
        _Resp(429, text="too many requests"),
        _Resp(200, {"ok": True}),
    ]

    def _fake_post(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr("ae.controller.etcd_state.requests.post", _fake_post)
    monkeypatch.setattr("ae.controller.etcd_state.time.sleep", lambda _s: None)
    monkeypatch.setenv("AE_ETCD_RETRY_MAX", "2")
    client = EtcdHttpClient(["http://127.0.0.1:2379"])

    out = client._post("/kv/put", {"key": "a", "value": "b"})

    assert out == {"ok": True}
    assert len(calls) == 2
    assert all("/v3/kv/put" in u for u in calls)
    assert not any("/v3alpha/kv/put" in u for u in calls)


def test_post_429_raises_non404_error_and_no_v3alpha(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_post(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return _Resp(429, text="too many requests")

    monkeypatch.setattr("ae.controller.etcd_state.requests.post", _fake_post)
    monkeypatch.setattr("ae.controller.etcd_state.time.sleep", lambda _s: None)
    monkeypatch.setenv("AE_ETCD_RETRY_MAX", "2")
    client = EtcdHttpClient(["http://127.0.0.1:2379"])

    with pytest.raises(RuntimeError) as exc:
        client._post("/kv/put", {"key": "a", "value": "b"})

    msg = str(exc.value)
    assert "status 429" in msg
    assert "v3alpha" not in msg
    assert all("/v3/kv/put" in u for u in calls)


def test_post_retries_connection_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    state = {"i": 0}

    def _fake_post(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        if state["i"] == 0:
            state["i"] += 1
            raise requests.ConnectionError("connection reset")
        return _Resp(200, {"ok": True})

    monkeypatch.setattr("ae.controller.etcd_state.requests.post", _fake_post)
    monkeypatch.setattr("ae.controller.etcd_state.time.sleep", lambda _s: None)
    monkeypatch.setenv("AE_ETCD_RETRY_MAX", "2")
    client = EtcdHttpClient(["http://127.0.0.1:2379"])

    out = client._post("/kv/range", {"key": "a"})

    assert out == {"ok": True}
    assert len(calls) == 2
    assert all("/v3/kv/range" in u for u in calls)

def test_post_429_retries_record_backoff_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    responses = [
        _Resp(429, text="too many requests"),
        _Resp(429, text="too many requests"),
        _Resp(200, {"ok": True}),
    ]

    def _fake_post(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return responses.pop(0)

    def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ae.controller.etcd_state.requests.post", _fake_post)
    monkeypatch.setattr("ae.controller.etcd_state.time.sleep", _fake_sleep)
    monkeypatch.setenv("AE_ETCD_RETRY_MAX", "3")
    monkeypatch.setenv("AE_ETCD_RETRY_BACKOFF", "10ms")
    monkeypatch.setenv("AE_ETCD_RETRY_JITTER", "0")
    client = EtcdHttpClient(["http://127.0.0.1:2379"])

    out = client._post("/kv/put", {"key": "a", "value": "b"})

    assert out == {"ok": True}
    assert len(calls) == 3
    assert all("/v3/kv/put" in u for u in calls)
    assert sleeps == [0.01, 0.02]
