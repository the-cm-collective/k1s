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

    def fake_get(url, headers, timeout, verify):  # noqa: ANN001
        _ = timeout
        assert "Authorization" in headers
        assert url.endswith("/status")
        assert verify is True
        return FakeResp({"items": []})

    import requests  # type: ignore

    monkeypatch.setattr(requests, "get", fake_get)
    data = _http_get_json("http://127.0.0.1:9108", "/status", token="t")
    assert "items" in data


def test_http_helpers_default_timeout(monkeypatch):
    from ae.cli.__main__ import _http_get_json

    captured = {}

    def fake_get(_url, headers, timeout, verify):  # noqa: ANN001
        _ = (headers, verify)
        captured["timeout"] = timeout
        return FakeResp({"items": []})

    import requests  # type: ignore

    monkeypatch.delenv("AE_CLI_HTTP_TIMEOUT", raising=False)
    monkeypatch.setattr(requests, "get", fake_get)

    _http_get_json("http://127.0.0.1:9108", "/status", token="t")

    assert captured["timeout"] == 10.0


def test_http_helpers_post(monkeypatch):
    from ae.cli.__main__ import _http_post_json

    def fake_post(_url, headers, json, timeout, verify):  # noqa: ANN001
        _ = timeout
        assert "Authorization" in headers
        assert json == {"replicas": 2}
        assert verify is True
        return FakeResp({"replicas": 2, "revision": 1, "status": "ready"})

    import requests  # type: ignore

    monkeypatch.setattr(requests, "post", fake_post)
    data = _http_post_json("http://127.0.0.1:9108", "/scale/echo", {"replicas": 2}, token="t")
    assert data["replicas"] == 2


def test_http_helpers_timeout_env(monkeypatch):
    from ae.cli.__main__ import _http_post_json

    captured = {}

    def fake_post(_url, headers, json, timeout, verify):  # noqa: ANN001
        _ = (headers, verify)
        captured["timeout"] = timeout
        return FakeResp({"ok": True, "body": json})

    import requests  # type: ignore

    monkeypatch.setenv("AE_CLI_HTTP_TIMEOUT", "240")
    monkeypatch.setattr(requests, "post", fake_post)

    _http_post_json("http://127.0.0.1:9108", "/apply", {"kind": "Deployment"}, token="t")

    assert captured["timeout"] == 240.0


# ruff: noqa: S106
