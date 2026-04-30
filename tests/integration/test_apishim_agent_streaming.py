"""Integration test: apishim exec + port-forward via node agent."""

from __future__ import annotations

import base64
import os
import socket
import socketserver
import threading
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer
from types import SimpleNamespace

import pytest

from ae.apishim.server import ShimServer
from ae.controller.state import NodeRecord
from ae.runtime.base import RuntimeAdapter, RuntimeResult
from ae.controller.spec import AppManifest
from ae.node.server import AgentHandler


class DummyRuntime(RuntimeAdapter):
    """Runtime stub used by the node agent."""

    def __init__(self, echo_port: int) -> None:
        self._echo_port = int(echo_port)

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
    ):
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
                "running": True,
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

        def _send() -> None:
            try:
                payload = b"pong"
                header = bytearray(8)
                header[0] = 1  # stdout
                header[4:8] = len(payload).to_bytes(4, "big")
                child_sock.sendall(bytes(header) + payload)
            finally:
                try:
                    child_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    child_sock.close()
                except Exception:
                    pass

        threading.Thread(target=_send, daemon=True).start()
        return parent_sock, exec_id

    def exec_exit_code(self, _exec_id: str) -> int:
        return 0


class EmptyRuntime(RuntimeAdapter):
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
        _ = (manifest, revision, keep_old, limit_create, pod_names, node_id)
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
        follow: bool = False,
        tail: int | None = None,
        since: int | None = None,
    ):
        _ = (follow, tail, since)
        return iter(())

    def ensure_storage_volumes(self, _app_name: str, _volumes: list[dict]) -> None:  # pragma: no cover
        return None

    def remove_storage_volumes(self, _app_name: str, _names: list[str]) -> int:  # pragma: no cover
        return 0

    def list_storage_volumes(self, _app_name: str | None = None) -> list[dict]:  # pragma: no cover
        return []

    def list_containers_info(self) -> list[dict]:
        return []


class RemotePodState:
    def __init__(self, agent_url: str) -> None:
        now = datetime.now(timezone.utc)
        self.node = NodeRecord(
            node_id="node-test",
            name="node-test",
            labels={"role": "hub"},
            capabilities={},
            taints=[],
            backend="cri",
            endpoint=agent_url,
            pod_cidr="10.42.0.0/24",
            wg_pubkey=None,
            rp_pubkey=None,
            cordoned=False,
            created_at=now,
            updated_at=now,
        )

    def list_status(self):
        return [SimpleNamespace(app_name="demo")]

    def list_pod_nodes(self, app_name: str):
        if app_name != "demo":
            return []
        return [("demo-pod", "node-test", True, True, "running", "ready", "live")]

    def get_node(self, node_id: str):
        if node_id != self.node.node_id:
            return None
        return self.node, None

    def list_nodes(self):
        return [(self.node, None)]


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):  # noqa: ANN001
        while True:
            data = self.request.recv(4096)
            if not data:
                break
            self.request.sendall(data)


def _start_echo_server():
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


def _start_apishim(monkeypatch: pytest.MonkeyPatch, tmp_path, agent_url: str):
    monkeypatch.setenv("AE_APISHIM_DB", str(tmp_path / "apishim.db"))
    monkeypatch.setenv("AE_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("AE_APISHIM_ADAPTER", "0")
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_APISHIM_AGENT_URL", agent_url)
    monkeypatch.setenv("AE_APISHIM_CRI_PORTFORWARD", "1")
    try:
        server = ShimServer(("127.0.0.1", 0), token=None, allow_anonymous=True)
    except PermissionError:
        pytest.skip("listener sockets not permitted in sandbox")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _ws_handshake(host: str, port: int, path: str, protocols: list[str]) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
    ]
    if protocols:
        headers.append("Sec-WebSocket-Protocol: " + ", ".join(protocols))
    headers.append("\r\n")
    sock.sendall("\r\n".join(headers).encode("utf-8"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("ws handshake failed")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b"101" not in status:
        raise RuntimeError(f"ws handshake failed: {status.decode('utf-8', 'ignore')}")
    return sock


def _ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x2) -> None:
    mask = os.urandom(4)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _ws_recv(sock: socket.socket) -> tuple[int, bytes] | None:
    hdr = sock.recv(2)
    if not hdr:
        return None
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        length = int.from_bytes(sock.recv(2), "big")
    elif length == 127:
        length = int.from_bytes(sock.recv(8), "big")
    mask = sock.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def test_apishim_exec_and_portforward(monkeypatch: pytest.MonkeyPatch, tmp_path):
    echo_server, echo_thread = _start_echo_server()
    echo_port = echo_server.server_address[1]
    agent_runtime = DummyRuntime(echo_port=echo_port)
    agent, agent_thread = _start_agent(agent_runtime)
    apishim, apishim_thread = _start_apishim(
        monkeypatch, tmp_path, f"http://127.0.0.1:{agent.server_port}"
    )
    try:
        # Exec over WebSocket
        exec_path = (
            "/api/v1/namespaces/default/pods/demo-pod/exec"
            "?command=echo&command=hi&stdin=0&stdout=1&stderr=1&tty=0"
        )
        ws = _ws_handshake("127.0.0.1", apishim.server_port, exec_path, ["v4.channel.k8s.io"])
        ws.settimeout(2)
        out = b""
        while True:
            msg = _ws_recv(ws)
            if msg is None:
                break
            _opcode, payload = msg
            if not payload:
                continue
            channel = payload[0]
            data = payload[1:]
            if channel == 1:
                out += data
                break
        ws.close()
        assert b"pong" in out

        # Port-forward over WebSocket
        pf_path = "/api/v1/namespaces/default/pods/demo-pod/portforward?ports=8080"
        pf = _ws_handshake(
            "127.0.0.1", apishim.server_port, pf_path, ["portforward.k8s.io"]
        )
        pf.settimeout(2)
        _ws_send(pf, b"\x00hello", opcode=0x2)
        msg = _ws_recv(pf)
        assert msg is not None
        _opcode, payload = msg
        assert payload[0:1] == b"\x00"
        assert payload[1:] == b"hello"
        pf.close()
    finally:
        apishim.shutdown()
        apishim.server_close()
        apishim_thread.join(timeout=2)
        agent.shutdown()
        agent.server_close()
        agent_thread.join(timeout=2)
        echo_server.shutdown()
        echo_server.server_close()
        echo_thread.join(timeout=2)


def test_apishim_exec_and_portforward_via_remote_pod_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    echo_server, echo_thread = _start_echo_server()
    echo_port = echo_server.server_address[1]
    agent_runtime = DummyRuntime(echo_port=echo_port)
    agent, agent_thread = _start_agent(agent_runtime)
    apishim, apishim_thread = _start_apishim(monkeypatch, tmp_path, "")
    try:
        apishim.runtime = EmptyRuntime()
        apishim._runtime_base = EmptyRuntime()
        apishim._runtime_cache = {}
        state = RemotePodState(f"http://127.0.0.1:{agent.server_port}")
        apishim.state = state

        pods_resp = socket.create_connection(("127.0.0.1", apishim.server_port), timeout=5)
        try:
            req = (
                "GET /api/v1/namespaces/default/pods HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{apishim.server_port}\r\n"
                "Connection: close\r\n\r\n"
            )
            pods_resp.sendall(req.encode("utf-8"))
            raw = b""
            while True:
                chunk = pods_resp.recv(4096)
                if not chunk:
                    break
                raw += chunk
        finally:
            pods_resp.close()
        assert b"demo-pod" in raw

        exec_path = (
            "/api/v1/namespaces/default/pods/demo-pod/exec"
            "?command=echo&command=hi&stdin=0&stdout=1&stderr=1&tty=0"
        )
        ws = _ws_handshake("127.0.0.1", apishim.server_port, exec_path, ["v4.channel.k8s.io"])
        ws.settimeout(2)
        out = b""
        while True:
            msg = _ws_recv(ws)
            if msg is None:
                break
            _opcode, payload = msg
            if not payload:
                continue
            channel = payload[0]
            data = payload[1:]
            if channel == 1:
                out += data
                break
        ws.close()
        assert b"pong" in out

        pf_path = "/api/v1/namespaces/default/pods/demo-pod/portforward?ports=8080"
        pf = _ws_handshake(
            "127.0.0.1", apishim.server_port, pf_path, ["portforward.k8s.io"]
        )
        pf.settimeout(2)
        _ws_send(pf, b"\x00hello", opcode=0x2)
        msg = _ws_recv(pf)
        assert msg is not None
        _opcode, payload = msg
        assert payload[0:1] == b"\x00"
        assert payload[1:] == b"hello"
        pf.close()
    finally:
        apishim.shutdown()
        apishim.server_close()
        apishim_thread.join(timeout=2)
        agent.shutdown()
        agent.server_close()
        agent_thread.join(timeout=2)
        echo_server.shutdown()
        echo_server.server_close()
        echo_thread.join(timeout=2)
