from __future__ import annotations

import io
import json
import os
from typing import Any

from ae.observability.http_api import _ApiHandler


class _Headers:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = {str(k): str(v) for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:  # noqa: D401
        return self._m.get(key, default)


def _make_handler(
    path: str, payload: dict[str, Any], *, labs: bool = True
) -> tuple[_ApiHandler, io.BytesIO, list[int]]:
    """Construct a minimal _ApiHandler with in-memory IO for POST handlers."""
    body = json.dumps(payload).encode("utf-8")
    rfile = io.BytesIO(body)
    wfile = io.BytesIO()

    # Build a bare handler object
    h = object.__new__(_ApiHandler)
    h.path = path  # type: ignore[attr-defined]
    h.headers = _Headers({"Content-Type": "application/json", "Content-Length": str(len(body))})  # type: ignore[attr-defined]
    h.rfile = rfile  # type: ignore[attr-defined]
    h.wfile = wfile  # type: ignore[attr-defined]

    status: list[int] = []

    def _send_response(code: int) -> None:
        status.clear()
        status.append(int(code))

    def _noop(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        return None

    h.send_response = _send_response  # type: ignore[attr-defined]
    h.send_header = _noop  # type: ignore[attr-defined]
    h.end_headers = _noop  # type: ignore[attr-defined]

    # Enable labs via env var for the handler’s guard
    if labs:
        os.environ["AE_LABS"] = "1"
        os.environ.pop("AE_LABS_TOKEN", None)  # no token required for tests

    return h, wfile, status


def test_labs_ingress_check_rejects_non_home_arpa() -> None:  # noqa: D401
    # Non-home.arpa host should be rejected by guard without network access
    h, wfile, status = _make_handler("/labs/ingress_check", {"url": "https://example.com/"})
    _ApiHandler._handle_labs_post(h)  # type: ignore[arg-type]
    assert status and status[0] == 400
    data = json.loads(wfile.getvalue().decode("utf-8") or "{}")
    assert "error" in data


def test_labs_ingress_check_returns_json_on_network_failure() -> None:  # noqa: D401
    # A dev host triggers a request; even if it fails, handler must return 200 with a JSON payload
    h, wfile, status = _make_handler(
        "/labs/ingress_check", {"url": "https://docs.home.arpa:8443/health"}
    )
    _ApiHandler._handle_labs_post(h)  # type: ignore[arg-type]
    assert status and status[0] == 200
    data = json.loads(wfile.getvalue().decode("utf-8") or "{}")
    assert set(["ok", "code", "elapsed_ms"]).issubset(set(data.keys()))
# ruff: noqa: C405
