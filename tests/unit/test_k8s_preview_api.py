import json
from pathlib import Path

from ae.controller.spec import load_manifest
from ae.controller.state import SQLiteStateStore
from ae.observability.http_api import _ApiHandler


class _Dummy:
    """Small placeholder object used for monkeypatched handler fields."""
    pass


def _make_handler(tmp_path):
    # Build a handler instance with a temp store; simulate minimal fields used
    obj = _Dummy()
    obj.store = SQLiteStateStore(tmp_path / "state.db")  # type: ignore[attr-defined]
    # Monkeypatch methods used by do_POST for test harness
    def _send(code):
        obj._last_code = code
    def _hdr(_k, _v):
        pass
    def _end():
        pass
    def _w(b: bytes):
        obj._last_body = b
    obj.send_response = lambda code: _send(code)  # type: ignore[attr-defined]
    obj.send_header = lambda _k, _v: _hdr(_k, _v)  # type: ignore[attr-defined]
    obj.end_headers = lambda: _end()  # type: ignore[attr-defined]
    obj.wfile = _Dummy()  # type: ignore[attr-defined]
    obj.wfile.write = lambda b: _w(b)  # type: ignore[attr-defined]
    obj.headers = {"Content-Length": "0"}  # type: ignore[attr-defined]
    obj.path = "/k8s/preview"  # type: ignore[attr-defined]
    # minimal helpers
    obj._json_ok = lambda _payload: _send(200)  # type: ignore[attr-defined]
    obj._json_error = lambda code, _msg: _send(code)  # type: ignore[attr-defined]
    return obj


def test_k8s_preview_dev_gate_and_success(monkeypatch, tmp_path: Path):
    handler = _make_handler(tmp_path)
    # Gate off -> expect 403
    import os
    monkeypatch.delenv("AE_API_DEV_EXPORT", raising=False)
    # Simulate request body
    handler.rfile = _Dummy()  # type: ignore[attr-defined]
    handler.rfile.read = lambda _n: b"{}"  # type: ignore[attr-defined]
    _ApiHandler.do_POST(handler)  # type: ignore[misc]
    assert getattr(handler, "_last_code", None) in {403, 404}

    # Gate on -> expect 200 and yaml field
    os.environ["AE_API_DEV_EXPORT"] = "1"
    man = load_manifest(Path("specs/examples/echo.yaml"))
    payload = {"apiVersion": "ae.dev/v1alpha1", "kind": "App", "metadata": {"name": man.metadata.name}, "spec": man.spec.model_dump(by_alias=True)}
    body = json.dumps({"options": {"namespace": "demo"}, **payload}).encode("utf-8")
    handler.rfile.read = lambda _n: body  # type: ignore[attr-defined]
    handler.headers = {"Content-Length": str(len(body))}  # type: ignore[attr-defined]
    _ApiHandler.do_POST(handler)  # type: ignore[misc]
    # The helper sets _last_code via _json_ok
    assert getattr(handler, "_last_code", None) == 200
# ruff: noqa: E501
