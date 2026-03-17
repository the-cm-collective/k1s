from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from ae.ha.fencing import MutationEnvelope, SQLiteFenceStore
from ae.node.server import AgentHandler, _json_response
from ae.runtime import RuntimeResult


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
        self._ensure_fence_store = AgentHandler._ensure_fence_store
        self._fence_scope = AgentHandler._fence_scope.__get__(self, AgentHandler)
        self._stale_response = AgentHandler._stale_response.__get__(self, AgentHandler)
        self._begin_mutation = AgentHandler._begin_mutation.__get__(self, AgentHandler)
        self._commit_mutation = AgentHandler._commit_mutation.__get__(self, AgentHandler)

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
        self.ensure_calls = 0
        self.remove_calls = 0
        self.remove_old_calls = 0

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

    def ensure_app(self, manifest, revision, **kwargs):
        self.ensure_calls += 1
        return RuntimeResult(revision=revision, created=1, updated=0, removed=0, pod_states=[])

    def remove_app(self, app_name: str) -> int:
        self.remove_calls += 1
        return 1

    def remove_old_revisions(self, app_name: str, keep_revision: int) -> int:
        self.remove_old_calls += 1
        return 2


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


def test_ensure_app_duplicate_returns_noop_after_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    fence_path = tmp_path / "fence.db"
    AgentHandler.fence_store = SQLiteFenceStore(fence_path)
    AgentHandler.fence_store.init()
    AgentHandler.fence_store.commit(
        "runtime:node-test",
        MutationEnvelope("ctrl-a", 7, "ensure:demo:3:node-test"),
    )
    AgentHandler.fence_store = SQLiteFenceStore(fence_path)
    AgentHandler.fence_store.init()

    runtime = _RuntimeStub()
    wfile = io.BytesIO()
    handler = _JsonBodyHandler(
        path="/v1/ensure_app",
        payload={
            "manifest": {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo"},
                "spec": {"image": "alpine:3.20", "replicas": 1},
            },
            "revision": 3,
            "node_id": "node-test",
            "controller_id": "ctrl-a",
            "controller_epoch": 7,
            "operation_id": "ensure:demo:3:node-test",
        },
        wfile=wfile,
        runtime=runtime,
    )

    AgentHandler.do_POST(handler)  # type: ignore[arg-type]

    assert runtime.ensure_calls == 0
    body = json.loads(wfile.getvalue().decode("utf-8"))
    assert body["duplicate"] is True
    assert handler.status_codes == [200]


def test_remove_app_rejects_stale_epoch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AE_HA_MODE", "1")
    AgentHandler.fence_store = SQLiteFenceStore(tmp_path / "fence.db")
    AgentHandler.fence_store.init()
    AgentHandler.fence_store.commit(
        "runtime:node-test",
        MutationEnvelope("ctrl-b", 9, "ensure:demo:3:node-test"),
    )

    runtime = _RuntimeStub()
    wfile = io.BytesIO()
    handler = _JsonBodyHandler(
        path="/v1/remove_app",
        payload={
            "app": "demo",
            "controller_id": "ctrl-a",
            "controller_epoch": 8,
            "operation_id": "delete:demo:8:node-test",
        },
        wfile=wfile,
        runtime=runtime,
    )

    AgentHandler.do_POST(handler)  # type: ignore[arg-type]

    assert runtime.remove_calls == 0
    body = json.loads(wfile.getvalue().decode("utf-8"))
    assert body["error"] == "stale_epoch"
    assert body["controller_id"] == "ctrl-b"
    assert body["controller_epoch"] == 9
    assert handler.status_codes == [409]
