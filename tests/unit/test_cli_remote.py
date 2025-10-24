import types

import pytest


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def test_http_helpers_get(monkeypatch):
    from ae.cli.__main__ import _http_get_json

    def fake_get(url, headers, timeout):  # noqa: ANN001
        assert "Authorization" in headers
        assert url.endswith("/status")
        return FakeResp({"items": []})

    import requests  # type: ignore

    monkeypatch.setattr(requests, "get", fake_get)
    data = _http_get_json("http://127.0.0.1:9108", "/status", token="t")
    assert "items" in data


def test_http_helpers_post(monkeypatch):
    from ae.cli.__main__ import _http_post_json

    def fake_post(url, headers, json, timeout):  # noqa: ANN001
        assert "Authorization" in headers
        assert json == {"replicas": 2}
        return FakeResp({"replicas": 2, "revision": 1, "status": "ready"})

    import requests  # type: ignore

    monkeypatch.setattr(requests, "post", fake_post)
    data = _http_post_json("http://127.0.0.1:9108", "/scale/echo", {"replicas": 2}, token="t")
    assert data["replicas"] == 2
