"""Integration test: apishim exec + port-forward via node agent."""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from ae.apishim import server as shim_server
from ae.apishim.server import ShimServer
from ae.controller.state import NodeRecord
from ae.runtime.base import RuntimeAdapter, RuntimeResult
from ae.controller.spec import AppManifest
from ae.node.server import AgentHandler


SPDY_DICT = base64.b64decode(
    "AAAAB29wdGlvbnMAAAAEaGVhZAAAAARwb3N0AAAAA3B1dAAAAAZkZWxldGUAAAAFdHJhY2UAAAAGYWNjZXB0AAAADmFjY2VwdC1jaGFyc2V0AAAAD2FjY2VwdC1lbmNvZGluZwAAAA9hY2NlcHQtbGFuZ3VhZ2UAAAANYWNjZXB0LXJhbmdlcwAAAANhZ2UAAAAFYWxsb3cAAAANYXV0aG9yaXphdGlvbgAAAA1jYWNoZS1jb250cm9sAAAACmNvbm5lY3Rpb24AAAAMY29udGVudC1iYXNlAAAAEGNvbnRlbnQtZW5jb2RpbmcAAAAQY29udGVudC1sYW5ndWFnZQAAAA5jb250ZW50LWxlbmd0aAAAABBjb250ZW50LWxvY2F0aW9uAAAAC2NvbnRlbnQtbWQ1AAAADWNvbnRlbnQtcmFuZ2UAAAAMY29udGVudC10eXBlAAAABGRhdGUAAAAEZXRhZwAAAAZleHBlY3QAAAAHZXhwaXJlcwAAAARmcm9tAAAABGhvc3QAAAAIaWYtbWF0Y2gAAAARaWYtbW9kaWZpZWQtc2luY2UAAAANaWYtbm9uZS1tYXRjaAAAAAhpZi1yYW5nZQAAABNpZi11bm1vZGlmaWVkLXNpbmNlAAAADWxhc3QtbW9kaWZpZWQAAAAIbG9jYXRpb24AAAAMbWF4LWZvcndhcmRzAAAABnByYWdtYQAAABJwcm94eS1hdXRoZW50aWNhdGUAAAATcHJveHktYXV0aG9yaXphdGlvbgAAAAVyYW5nZQAAAAdyZWZlcmVyAAAAC3JldHJ5LWFmdGVyAAAABnNlcnZlcgAAAAJ0ZQAAAAd0cmFpbGVyAAAAEXRyYW5zZmVyLWVuY29kaW5nAAAAB3VwZ3JhZGUAAAAKdXNlci1hZ2VudAAAAAR2YXJ5AAAAA3ZpYQAAAAd3YXJuaW5nAAAAEHd3dy1hdXRoZW50aWNhdGUAAAAGbWV0aG9kAAAAA2dldAAAAAZzdGF0dXMAAAAGMjAwIE9LAAAAB3ZlcnNpb24AAAAISFRUUC8xLjEAAAADdXJsAAAABnB1YmxpYwAAAApzZXQtY29va2llAAAACmtlZXAtYWxpdmUAAAAGb3JpZ2luMTAwMTAxMjAxMjAyMjA1MjA2MzAwMzAyMzAzMzA0MzA1MzA2MzA3NDAyNDA1NDA2NDA3NDA4NDA5NDEwNDExNDEyNDEzNDE0NDE1NDE2NDE3NTAyNTA0NTA1MjAzIE5vbi1BdXRob3JpdGF0aXZlIEluZm9ybWF0aW9uMjA0IE5vIENvbnRlbnQzMDEgTW92ZWQgUGVybWFuZW50bHk0MDAgQmFkIFJlcXVlc3Q0MDEgVW5hdXRob3JpemVkNDAzIEZvcmJpZGRlbjQwNCBOb3QgRm91bmQ1MDAgSW50ZXJuYWwgU2VydmVyIEVycm9yNTAxIE5vdCBJbXBsZW1lbnRlZDUwMyBTZXJ2aWNlIFVuYXZhaWxhYmxlSmFuIEZlYiBNYXIgQXByIE1heSBKdW4gSnVsIEF1ZyBTZXB0IE9jdCBOb3YgRGVjIDAwOjAwOjAwIE1vbiwgVHVlLCBXZWQsIFRodSwgRnJpLCBTYXQsIFN1biwgR01UY2h1bmtlZCx0ZXh0L2h0bWwsaW1hZ2UvcG5nLGltYWdlL2pwZyxpbWFnZS9naWYsYXBwbGljYXRpb24veG1sLGFwcGxpY2F0aW9uL3hodG1sK3htbCx0ZXh0L3BsYWluLHRleHQvamF2YXNjcmlwdCxwdWJsaWNwcml2YXRlbWF4LWFnZT1nemlwLGRlZmxhdGUsc2RjaGNoYXJzZXQ9dXRmLThjaGFyc2V0PWlzby04ODU5LTEsdXRmLSwqLGVucT0wLg=="
)


class DummyRuntime(RuntimeAdapter):
    """Runtime stub used by the node agent."""

    def __init__(
        self,
        echo_port: int,
        *,
        status_delay: float = 0.0,
        close_delay: float = 0.0,
    ) -> None:
        self._echo_port = int(echo_port)
        self._status_delay = float(status_delay)
        self._close_delay = float(close_delay)
        self._exec_done: dict[str, threading.Event] = {}

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
        done = threading.Event()
        self._exec_done[exec_id] = done

        def _send() -> None:
            try:
                payload = b"pong"
                header = bytearray(8)
                header[0] = 1  # stdout
                header[4:8] = len(payload).to_bytes(4, "big")
                child_sock.sendall(bytes(header) + payload)
                if self._status_delay > 0:
                    time.sleep(self._status_delay)
                done.set()
                if self._close_delay > 0:
                    time.sleep(self._close_delay)
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

    def exec_status(self, exec_id: str) -> tuple[bool, int | None] | None:
        done = self._exec_done.get(exec_id)
        if done is None:
            return None
        if done.is_set():
            return False, 0
        return True, None


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
        server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
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


def _ws_recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_recv(sock: socket.socket) -> tuple[int, bytes] | None:
    hdr = _ws_recv_exact(sock, 2)
    if not hdr:
        return None
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        ext = _ws_recv_exact(sock, 2)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = _ws_recv_exact(sock, 8)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    mask = _ws_recv_exact(sock, 4) if masked else b""
    payload = _ws_recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_collect_exec(sock: socket.socket, *, timeout: float = 5.0) -> tuple[bytes, bytes, dict | None, bool]:
    sock.settimeout(0.2)
    deadline = time.time() + timeout
    stdout = b""
    stderr = b""
    status = None
    close_seen = False
    while time.time() < deadline:
        try:
            msg = _ws_recv(sock)
        except socket.timeout:
            continue
        if msg is None:
            break
        opcode, payload = msg
        if opcode == 0x8:
            close_seen = True
            break
        if not payload:
            continue
        channel = payload[0]
        data = payload[1:]
        if channel == 1:
            stdout += data
        elif channel == 2:
            stderr += data
        elif channel == 3:
            status = json.loads(data.decode("utf-8"))
    return stdout, stderr, status, close_seen


def _spdy_handshake(
    host: str, port: int, path: str, protocols: list[str]
) -> tuple[socket.socket, bytes]:
    sock = socket.create_connection((host, port), timeout=5)
    headers = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: Upgrade",
        "Upgrade: SPDY/3.1",
        "Content-Length: 0",
        "X-Stream-Protocol-Version: " + ", ".join(protocols),
        "\r\n",
    ]
    sock.sendall("\r\n".join(headers).encode("utf-8"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("spdy handshake failed")
        buf += chunk
    header, rest = buf.split(b"\r\n\r\n", 1)
    status = header.split(b"\r\n", 1)[0]
    if b"101" not in status:
        raise RuntimeError(f"spdy handshake failed: {status.decode('utf-8', 'ignore')}")
    return sock, rest


def _spdy_encode_headers(cctx: zlib.compressobj, headers: dict[str, str]) -> bytes:
    buf = bytearray()
    buf += len(headers).to_bytes(4, "big")
    for name, value in headers.items():
        key = name.encode("utf-8")
        encoded = value.encode("utf-8")
        buf += len(key).to_bytes(4, "big")
        buf += key
        buf += len(encoded).to_bytes(4, "big")
        buf += encoded
    return cctx.compress(bytes(buf)) + cctx.flush(zlib.Z_SYNC_FLUSH)


def _spdy_send_syn_stream(
    sock: socket.socket,
    *,
    cctx: zlib.compressobj,
    stream_id: int,
    path: str,
    host: str,
    streamtype: str,
) -> None:
    hdrs = _spdy_encode_headers(
        cctx,
        {
            ":method": "POST",
            ":path": path,
            ":version": "HTTP/1.1",
            ":host": host,
            "streamtype": streamtype,
        },
    )
    payload = (stream_id & 0x7FFFFFFF).to_bytes(4, "big") + b"\x00\x00\x00\x00" + b"\x00\x00" + hdrs
    header = bytearray()
    header += b"\x80\x03"
    header += (0x01).to_bytes(2, "big")
    header += b"\x00"
    header += len(payload).to_bytes(3, "big")
    sock.sendall(bytes(header) + payload)


def _spdy_read_data_frames(
    sock: socket.socket,
    *,
    initial: bytes = b"",
    timeout: float = 5.0,
) -> dict[int, bytes]:
    sock.settimeout(0.2)
    deadline = time.time() + timeout
    buf = initial
    payloads: dict[int, bytes] = {}
    fin_streams: set[int] = set()
    while time.time() < deadline:
        if len(buf) < 8:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            continue
        hdr = buf[:8]
        length = int.from_bytes(hdr[5:8], "big")
        frame_len = 8 + length
        if len(buf) < frame_len:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            continue
        payload = buf[8:frame_len]
        buf = buf[frame_len:]
        if hdr[0] & 0x80:
            continue
        stream_id = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
        payloads[stream_id] = payloads.get(stream_id, b"") + payload
        if hdr[4] & 0x02:
            fin_streams.add(stream_id)
        if 1 in fin_streams and payloads.get(3):
            break
    return payloads


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
        out, err, status, close_seen = _ws_collect_exec(ws)
        ws.close()
        assert b"pong" in out
        assert err == b""
        assert status == {"metadata": {}, "status": "Success", "message": "", "reason": "", "code": 0, "details": {"exitCode": 0}}
        assert close_seen is True

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
        out, err, status, close_seen = _ws_collect_exec(ws)
        ws.close()
        assert b"pong" in out
        assert err == b""
        assert status == {"metadata": {}, "status": "Success", "message": "", "reason": "", "code": 0, "details": {"exitCode": 0}}
        assert close_seen is True

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


def test_apishim_exec_via_remote_pod_state_keeps_loopback_when_implicit_fallback_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.delenv("AE_NODE_ADVERTISE_IP", raising=False)
    orig_exists = shim_server.Path.exists

    def _exists(path_obj):
        if str(path_obj) == "/.dockerenv":
            return True
        return orig_exists(path_obj)

    orig_lookup = shim_server.socket.getaddrinfo

    def _lookup(host, port, *args, **kwargs):
        if host == "host.containers.internal":
            raise OSError("not resolvable")
        return orig_lookup(host, port, *args, **kwargs)

    monkeypatch.setattr(shim_server.Path, "exists", _exists)
    monkeypatch.setattr(shim_server.socket, "getaddrinfo", _lookup)

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

        exec_path = (
            "/api/v1/namespaces/default/pods/demo-pod/exec"
            "?command=echo&command=hi&stdin=0&stdout=1&stderr=1&tty=0"
        )
        ws = _ws_handshake("127.0.0.1", apishim.server_port, exec_path, ["v4.channel.k8s.io"])
        out, err, status, close_seen = _ws_collect_exec(ws)
        ws.close()
        assert b"pong" in out
        assert err == b""
        assert status == {
            "metadata": {},
            "status": "Success",
            "message": "",
            "reason": "",
            "code": 0,
            "details": {"exitCode": 0},
        }
        assert close_seen is True
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


def test_apishim_exec_spdy_buffers_stdout_until_stream_registration(
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

        exec_path = (
            "/api/v1/namespaces/default/pods/demo-pod/exec"
            "?command=echo&command=hi&stdin=0&stdout=1&stderr=0&tty=0"
        )
        sock, initial = _spdy_handshake(
            "127.0.0.1",
            apishim.server_port,
            exec_path,
            ["v4.channel.k8s.io"],
        )
        cctx = zlib.compressobj(wbits=15, zdict=SPDY_DICT)
        try:
            _spdy_send_syn_stream(
                sock,
                cctx=cctx,
                stream_id=1,
                path=exec_path,
                host="127.0.0.1",
                streamtype="error",
            )
            time.sleep(0.2)
            _spdy_send_syn_stream(
                sock,
                cctx=cctx,
                stream_id=3,
                path=exec_path,
                host="127.0.0.1",
                streamtype="stdout",
            )
            payloads = _spdy_read_data_frames(sock, initial=initial)
        finally:
            sock.close()

        assert b"pong" in payloads.get(3, b"")
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


def test_apishim_exec_ws_completes_before_attach_close_via_remote_pod_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    echo_server, echo_thread = _start_echo_server()
    echo_port = echo_server.server_address[1]
    agent_runtime = DummyRuntime(echo_port=echo_port, close_delay=3.0)
    agent, agent_thread = _start_agent(agent_runtime)
    apishim, apishim_thread = _start_apishim(monkeypatch, tmp_path, "")
    try:
        apishim.runtime = EmptyRuntime()
        apishim._runtime_base = EmptyRuntime()
        apishim._runtime_cache = {}
        state = RemotePodState(f"http://127.0.0.1:{agent.server_port}")
        apishim.state = state

        exec_path = (
            "/api/v1/namespaces/default/pods/demo-pod/exec"
            "?command=echo&command=hi&stdin=0&stdout=1&stderr=1&tty=0"
        )
        ws = _ws_handshake("127.0.0.1", apishim.server_port, exec_path, ["v4.channel.k8s.io"])
        out, err, status, close_seen = _ws_collect_exec(ws, timeout=2.0)
        ws.close()

        assert out == b"pong"
        assert err == b""
        assert status == {
            "metadata": {},
            "status": "Success",
            "message": "",
            "reason": "",
            "code": 0,
            "details": {"exitCode": 0},
        }
        assert close_seen is True
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
