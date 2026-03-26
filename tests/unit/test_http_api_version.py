from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

from ae.observability import http_api


def make_handler(path: str):
    class DummyRequest:
        def __init__(self):
            self._raw = f"GET {path} HTTP/1.1\r\n\r\n".encode()
            self._wbuf = bytearray()
            self.path = path
            self.command = "GET"
            self.headers = {}
            self.responses: list[int] = []

        def send_response(self, code, _message=None):
            self.responses.append(code)

        def send_header(self, _k, _v):
            pass

        def end_headers(self):
            pass

        def makefile(self, mode, *_args, **_kwargs):
            if "r" in mode:
                return BytesIO(self._raw)
            return BytesIO(self._wbuf)

        def settimeout(self, _t):
            pass

        def setsockopt(self, *a, **k):
            _ = (a, k)
            pass

        def close(self):
            pass

        def sendall(self, data):
            self._wbuf.extend(data)

        @property
        def rfile(self):
            return BytesIO(self._raw)

        @property
        def wfile(self):
            return SimpleNamespace(write=lambda b: self._wbuf.extend(b))

    return DummyRequest()


def test_controller_internal_version_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("AE_BUILD_SHA", "sha-123")
    monkeypatch.setenv("AE_BUILD_DATE", "2026-03-18")
    req = make_handler("/__ae/version")
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.do_GET()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    assert 200 in req.responses
    assert '"component": "controller"' in body
    assert '"sha": "sha-123"' in body
    assert '"date": "2026-03-18"' in body


def test_ui_features_report_controlplane_readonly_flags(monkeypatch) -> None:
    monkeypatch.setenv("AE_PLAYGROUND", "1")
    monkeypatch.setenv("AE_CONTROLPLANE_PUBLIC_ENABLE", "1")
    monkeypatch.setenv("AE_CONTROLPLANE_AUTH_ENABLE", "1")
    monkeypatch.setenv("AE_DASHBOARD_INTERACTIVE_TOOLS", "0")

    req = make_handler("/ui/features")
    handler = http_api._ApiHandler(req, ("127.0.0.1", 0), None)
    handler.do_GET()

    body = bytes(req._wbuf).decode("utf-8", errors="ignore")
    payload = json.loads(body.rsplit("\r\n\r\n", 1)[1])
    assert 200 in req.responses
    assert payload["playground"] is False
    assert payload["dashboard_interactive_tools"] is False
