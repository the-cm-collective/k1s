"""Integration test for agent-backed streaming exec and port-forward."""

from __future__ import annotations

import socket
import socketserver
import threading
import uuid
from collections.abc import Iterable

import pytest

from ae.controller.spec import AppManifest
from ae.runtime.base import RuntimeAdapter, RuntimeResult
from ae.runtime.remote_runtime import RemoteRuntime
from ae.node.server import AgentHandler
from http.server import HTTPServer


class DummyLocalRuntime(RuntimeAdapter):
    def ensure_app(  # pragma: no cover - unused in this test
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        return RuntimeResult(revision=revision, created=0, updated=0, removed=0, pod_states=[])

    def remove_app(self, _app_name: str) -> int:  # pragma: no cover - unused in this test
        return 0

    def remove_old_revisions(  # pragma: no cover - unused in this test
        self, _app_name: str, _keep_revision: int
    ) -> int:
        return 0

    def read_logs(  # pragma: no cover - unused in this test
        self,
        _pod_name: str,
        *,
        _follow: bool = False,
        _tail: int | None = None,
        _since: int | None = None,
    ) -> Iterable[str]:
        return iter(())

    def ensure_storage_volumes(self, _app_name: str, _volumes: list[dict]) -> None:  # pragma: no cover
        return None

    def remove_storage_volumes(self, _app_name: str, _names: list[str]) -> int:  # pragma: no cover
        return 0

    def list_storage_volumes(self, _app_name: str | None = None) -> list[dict]:  # pragma: no cover
        return []

    def list_containers_info(self) -> list[dict]:  # pragma: no cover
        return []

    def exec(self, _pod_name: str, _command: list[str], *, _timeout: int | None = None) -> int:
        return 0


class DummyRuntime(RuntimeAdapter):
    """Runtime stub that supports exec_attach + port-forward target discovery."""

    def __init__(self, echo_port: int) -> None:
        self._echo_port = int(echo_port)
        self.resize_calls: list[tuple[str, int | None, int | None]] = []

    def ensure_app(  # pragma: no cover - unused in this test
        self,
        manifest: AppManifest,
        revision: int,
        *,
        keep_old: bool = False,
        limit_create: int | None = None,
        pod_names: list[str] | None = None,
        node_id: str | None = None,
    ) -> RuntimeResult:
        return RuntimeResult(revision=revision, created=0, updated=0, removed=0, pod_states=[])

    def remove_app(self, _app_name: str) -> int:  # pragma: no cover - unused in this test
        return 0

    def remove_old_revisions(  # pragma: no cover - unused in this test
        self, _app_name: str, _keep_revision: int
    ) -> int:
        return 0

    def read_logs(  # pragma: no cover - unused in this test
        self,
        _pod_name: str,
        *,
        _follow: bool = False,
        _tail: int | None = None,
        _since: int | None = None,
    ) -> Iterable[str]:
        return iter(())

    def ensure_storage_volumes(self, _app_name: str, _volumes: list[dict]) -> None:  # pragma: no cover
        return None

    def remove_storage_volumes(self, _app_name: str, _names: list[str]) -> int:  # pragma: no cover
        return 0

    def list_storage_volumes(self, _app_name: str | None = None) -> list[dict]:  # pragma: no cover
        return []

    def list_containers_info(self) -> list[dict]:
        return [
            {
                "name": "demo-pod",
                "labels": {"ae.pod_name": "demo-pod", "ae.namespace": "default"},
                "uid": "demo-pod-uid",
                "host_ip": "127.0.0.1",
                "pod_ip": None,
                "host_ports": [self._echo_port],
                "port_map": {8080: self._echo_port},
            }
        ]

    def exec(self, _pod_name: str, _command: list[str], *, _timeout: int | None = None) -> int:
        return 0

    def exec_attach(
        self,
        _pod_name: str,
        _command: list[str],
        *,
        container: str | None = None,
        tty: bool = False,
    ):
        _ = (container, tty)
        parent_sock, child_sock = socket.socketpair()
        exec_id = uuid.uuid4().hex

        def _echo() -> None:
            try:
                while True:
                    data = child_sock.recv(4096)
                    if not data:
                        break
                    child_sock.sendall(data)
            finally:
                try:
                    child_sock.close()
                except Exception:
                    pass

        threading.Thread(target=_echo, daemon=True).start()
        return parent_sock, exec_id

    def exec_resize(self, exec_id: str, *, height: int | None = None, width: int | None = None) -> None:
        self.resize_calls.append((exec_id, height, width))

    def exec_exit_code(self, _exec_id: str) -> int:
        return 0


def _start_echo_server():
    class EchoHandler(socketserver.BaseRequestHandler):
        def handle(self):  # noqa: ANN001
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                self.request.sendall(data)

    try:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _start_agent(runtime: RuntimeAdapter):
    AgentHandler.runtime = runtime  # type: ignore[assignment]
    AgentHandler.node_id = "node-test"
    try:
        server = HTTPServer(("127.0.0.1", 0), AgentHandler)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_agent_streaming_exec_and_portforward():
    echo_server, echo_thread = _start_echo_server()
    echo_port = echo_server.server_address[1]
    runtime = DummyRuntime(echo_port=echo_port)
    agent, agent_thread = _start_agent(runtime)
    try:
        remote = RemoteRuntime(f"http://127.0.0.1:{agent.server_port}", DummyLocalRuntime())

        sock, exec_id = remote.exec_attach("demo-pod", ["echo", "hi"], tty=False)
        sock.settimeout(1)
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"
        sock.close()

        remote.exec_resize(exec_id or "", height=10, width=20)
        assert runtime.resize_calls[-1] == (exec_id, 10, 20)
        assert remote.exec_exit_code(exec_id or "") == 0

        pf_sock = remote.port_forward_socket(
            pod_id=None, pod_name="demo-pod", namespace="default", port=8080
        )
        pf_sock.settimeout(1)
        pf_sock.sendall(b"hello")
        assert pf_sock.recv(5) == b"hello"
        pf_sock.close()
    finally:
        agent.shutdown()
        agent.server_close()
        agent_thread.join(timeout=2)
        echo_server.shutdown()
        echo_server.server_close()
        echo_thread.join(timeout=2)
