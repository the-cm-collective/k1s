from __future__ import annotations

import io
import json
import logging

from ae.node.server import AgentHandler, _json_response


class _BrokenPipeWriter:
    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError(32, "broken pipe")


class _RuntimeErrorWriter:
    def write(self, _payload: bytes) -> int:
        raise RuntimeError("write failed")


class _JsonBodyHandler:
    def __init__(self, *, path: str, payload: dict, wfile, runtime) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = wfile
        self.runtime = runtime
        self.volume_manager = None
        self.node_id = "node-test"
        self.status_codes: list[int] = []

    def send_response(self, status: int, _message: str | None = None) -> None:
        self.status_codes.append(int(status))

    def send_header(self, _name: str, _value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None


class _RuntimeStub:
    def __init__(self) -> None:
        self.resize_calls: list[tuple[str, int | None, int | None]] = []
        self.inspect_calls: list[str] = []

    def exec_resize(
        self,
        exec_id: str,
        *,
        height: int | None = None,
        width: int | None = None,
    ) -> None:
        self.resize_calls.append((exec_id, height, width))

    def exec_exit_code(self, exec_id: str) -> int:
        self.inspect_calls.append(exec_id)
        return 7


def test_json_response_returns_false_on_broken_pipe() -> None:
    runtime = _RuntimeStub()
    handler = _JsonBodyHandler(
        path="/v1/exec_resize",
        payload={},
        wfile=_BrokenPipeWriter(),
        runtime=runtime,
    )
    assert _json_response(handler, 200, {"ok": True}) is False
    assert handler.status_codes == [200]


def test_json_response_raises_for_non_disconnect_write_error() -> None:
    runtime = _RuntimeStub()
    handler = _JsonBodyHandler(
        path="/v1/exec_resize",
        payload={},
        wfile=_RuntimeErrorWriter(),
        runtime=runtime,
    )
    try:
        _json_response(handler, 200, {"ok": True})
    except RuntimeError as exc:
        assert "write failed" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError")


def test_exec_resize_ignores_client_disconnect_without_error_log(caplog) -> None:
    runtime = _RuntimeStub()
    handler = _JsonBodyHandler(
        path="/v1/exec_resize",
        payload={"exec_id": "exec-1", "height": 30, "width": 120},
        wfile=_BrokenPipeWriter(),
        runtime=runtime,
    )
    caplog.set_level(logging.ERROR, logger="ae.node.server")
    AgentHandler.do_POST(handler)  # type: ignore[arg-type]
    assert runtime.resize_calls == [("exec-1", 30, 120)]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_exec_inspect_ignores_client_disconnect_without_error_log(caplog) -> None:
    runtime = _RuntimeStub()
    handler = _JsonBodyHandler(
        path="/v1/exec_inspect",
        payload={"exec_id": "exec-2"},
        wfile=_BrokenPipeWriter(),
        runtime=runtime,
    )
    caplog.set_level(logging.ERROR, logger="ae.node.server")
    AgentHandler.do_POST(handler)  # type: ignore[arg-type]
    assert runtime.inspect_calls == ["exec-2"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
