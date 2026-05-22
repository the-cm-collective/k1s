import argparse
import json
import socket
import ssl
import time

import pytest
import requests

from ae.cli import __main__ as cli
from ae.controller.state import SQLiteStateStore


class DummyRuntime:
    pass


def _ws_frame(payload: bytes, *, opcode: int = 0x2) -> bytes:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(127)
        header.extend(length.to_bytes(8, "big"))
    return bytes(header) + payload


class _ScriptedSocket:
    def __init__(self, script: list[bytes | BaseException]) -> None:
        self._script = list(script)
        self.sent: list[bytes] = []
        self.timeout: float | None = None

    def recv(self, n: int) -> bytes:
        if not self._script:
            return b""
        item = self._script[0]
        if isinstance(item, BaseException):
            self._script.pop(0)
            raise item
        if not item:
            self._script.pop(0)
            return b""
        data = item[:n]
        rest = item[n:]
        if rest:
            self._script[0] = rest
        else:
            self._script.pop(0)
        return data

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def close(self) -> None:
        return


def test_apishim_ssl_context_loads_ca_bundle(monkeypatch, tmp_path):
    ca_bundle = tmp_path / "apishim.ca.crt"
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    calls: dict[str, str] = {}

    class DummyContext:
        def __init__(self):
            self.check_hostname = True
            self.verify_mode = ssl.CERT_REQUIRED

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):
            calls["cafile"] = cafile

    dummy = DummyContext()
    monkeypatch.setenv("AE_APISHIM_CA_BUNDLE", str(ca_bundle))
    monkeypatch.delenv("AE_APISHIM_INSECURE", raising=False)
    monkeypatch.setattr(ssl, "create_default_context", lambda: dummy)

    ctx = cli._apishim_ssl_context()

    assert ctx is dummy
    assert calls["cafile"] == str(ca_bundle)
    assert dummy.check_hostname is True
    assert dummy.verify_mode == ssl.CERT_REQUIRED


def test_apishim_ssl_context_honors_insecure(monkeypatch):
    class DummyContext:
        def __init__(self):
            self.check_hostname = True
            self.verify_mode = ssl.CERT_REQUIRED
            self.loaded = False

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):
            self.loaded = True

    dummy = DummyContext()
    monkeypatch.setenv("AE_APISHIM_INSECURE", "1")
    monkeypatch.setenv("AE_APISHIM_CA_BUNDLE", "/tmp/demo-ca.pem")
    monkeypatch.setattr(ssl, "create_default_context", lambda: dummy)

    ctx = cli._apishim_ssl_context()

    assert ctx is dummy
    assert dummy.check_hostname is False
    assert dummy.verify_mode == ssl.CERT_NONE
    assert dummy.loaded is False


def test_http_get_json_uses_apishim_verify(monkeypatch, tmp_path):
    ca_bundle = tmp_path / "apishim.ca.crt"
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_get(url, headers, timeout, verify):
        captured["url"] = url
        captured["verify"] = verify
        return DummyResponse()

    monkeypatch.setenv("AE_APISHIM_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setattr(requests, "get", fake_get)

    payload = cli._http_get_json("https://127.0.0.1:8445", "/api/v1/namespaces/default/pods")

    assert payload == {"ok": True}
    assert captured["url"] == "https://127.0.0.1:8445/api/v1/namespaces/default/pods"
    assert captured["verify"] == str(ca_bundle)


def test_http_post_json_uses_apishim_verify(monkeypatch, tmp_path):
    ca_bundle = tmp_path / "apishim.ca.crt"
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_post(url, headers, json, timeout, verify):
        captured["url"] = url
        captured["json"] = json
        captured["verify"] = verify
        return DummyResponse()

    monkeypatch.setenv("AE_APISHIM_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setattr(requests, "post", fake_post)

    payload = cli._http_post_json(
        "https://127.0.0.1:8445",
        "/api/apishim/session",
        {"role": "exec"},
    )

    assert payload == {"ok": True}
    assert captured["url"] == "https://127.0.0.1:8445/api/apishim/session"
    assert captured["json"] == {"role": "exec"}
    assert captured["verify"] == str(ca_bundle)


def test_handle_exec_ws_fallback(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=True,
        timeout=None,
    )
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(
        cli, "_exec_over_spdy", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("spdy"))
    )
    called = {"ws": False}

    def _fake_ws(*_a, **_k):
        called["ws"] = True
        return 0

    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    assert called["ws"] is True


def test_handle_exec_reports_transport_fallback(monkeypatch, tmp_path, capsys):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_EXEC_TRANSPORT_REPORT", "1")
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(
        cli,
        "_exec_over_ws",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("The read operation timed out")),
    )
    monkeypatch.setattr(cli, "_exec_over_spdy", lambda *_a, **_k: 0)

    rc = cli.handle_exec(args, store, DummyRuntime())

    captured = capsys.readouterr()
    assert rc == 0
    assert "trying spdy fallback" in captured.out
    assert (
        "AE_EXEC_TRANSPORT_REPORT primary=websocket final=spdy fallback=1 status=ok"
        in captured.err
    )


def test_handle_exec_reports_primary_transport_success(monkeypatch, tmp_path, capsys):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_EXEC_TRANSPORT_REPORT", "1")
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(cli, "_exec_over_ws", lambda *_a, **_k: 0)

    rc = cli.handle_exec(args, store, DummyRuntime())

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert (
        "AE_EXEC_TRANSPORT_REPORT primary=websocket final=websocket fallback=0 status=ok"
        in captured.err
    )


def test_exec_over_ws_tolerates_late_status_after_output(monkeypatch, capsys):
    status = json.dumps(
        {"status": "Success", "details": {"exitCode": 0}},
        separators=(",", ":"),
    ).encode("utf-8")
    sock = _ScriptedSocket(
        [
            (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            ),
            _ws_frame(b"\x01hello\n"),
            socket.timeout("timed out"),
            _ws_frame(b"\x03" + status),
            _ws_frame(b"\x03\xe8", opcode=0x8),
        ]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: sock)

    rc = cli._exec_over_ws(
        "http://127.0.0.1:8445",
        namespace="default",
        pod_name="demo-pod",
        command=["echo", "hi"],
        container=None,
        stdin=False,
        stdout=True,
        stderr=True,
        tty=False,
        token=None,
        timeout=None,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "hello\n"
    assert sock.timeout == 1.0


def test_exec_over_ws_times_out_before_output(monkeypatch):
    sock = _ScriptedSocket(
        [
            (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
            ),
            socket.timeout("timed out"),
        ]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: sock)

    with pytest.raises(RuntimeError, match="timed out"):
        cli._exec_over_ws(
            "http://127.0.0.1:8445",
            namespace="default",
            pod_name="demo-pod",
            command=["echo", "hi"],
            container=None,
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
            token=None,
            timeout=None,
        )


def test_handle_exec_mints_session_token_when_missing(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    cache_path = tmp_path / "sessions.json"
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_APISHIM_MINT_TOKEN", "mint-token")
    monkeypatch.setenv("AE_APISHIM_SESSION_CACHE", str(cache_path))
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_EXEC_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )

    def _fake_mint(*_a, **_k):
        return {"token": "sess1.token-a.sig", "expires_at": int(time.time()) + 600}

    monkeypatch.setattr(cli, "_mint_apishim_session_token", _fake_mint)

    def _fake_ws(*_a, **kwargs):
        assert kwargs.get("token") == "sess1.token-a.sig"
        return 0

    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0


def test_handle_exec_refreshes_token_after_401(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    cache_path = tmp_path / "sessions.json"
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_APISHIM_MINT_TOKEN", "mint-token")
    monkeypatch.setenv("AE_APISHIM_SESSION_CACHE", str(cache_path))
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_EXEC_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    issued = iter(
        [
            {"token": "sess1.token-a.sig", "expires_at": int(time.time()) + 600},
            {"token": "sess1.token-b.sig", "expires_at": int(time.time()) + 600},
        ]
    )
    monkeypatch.setattr(cli, "_mint_apishim_session_token", lambda *_a, **_k: next(issued))
    calls: list[str | None] = []

    def _fake_ws(*_a, **kwargs):
        tok = kwargs.get("token")
        calls.append(tok)
        if len(calls) == 1:
            raise RuntimeError("websocket upgrade failed: 401")
        return 0

    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    assert calls == ["sess1.token-a.sig", "sess1.token-b.sig"]


def test_handle_exec_labs_fallback_after_401(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_LABS_TOKEN", "labs-token")
    monkeypatch.delenv("AE_APISHIM_MINT_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_EXEC_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(cli, "_resolve_apishim_stream_token", lambda **_k: "stale-token")
    monkeypatch.setattr(cli, "_resolve_labs_stream_token", lambda **_k: "sess1.labs.sig")
    calls: list[str | None] = []

    def _fake_ws(*_a, **kwargs):
        tok = kwargs.get("token")
        calls.append(tok)
        if len(calls) == 1:
            raise RuntimeError("websocket upgrade failed: 401")
        return 0

    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    assert calls == ["stale-token", "sess1.labs.sig"]


def test_handle_port_forward_labs_fallback_after_401(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        mapping="18080:8080",
        pod="echo-rev1-0",
        bind="127.0.0.1",
        apishim="http://127.0.0.1:8445",
        namespace=None,
    )
    monkeypatch.setenv("AE_LABS_TOKEN", "labs-token")
    monkeypatch.delenv("AE_APISHIM_MINT_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_PORTFORWARD_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_resolve_apishim_stream_token", lambda **_k: "stale-token")
    monkeypatch.setattr(cli, "_resolve_labs_stream_token", lambda **_k: "sess1.labs-pf.sig")
    calls: list[str | None] = []

    def _fake_pf(*_a, **kwargs):
        tok = kwargs.get("token")
        calls.append(tok)
        if len(calls) == 1:
            raise RuntimeError("websocket upgrade failed: 401")
        return 0

    monkeypatch.setattr(cli, "_portforward_over_ws", _fake_pf)
    rc = cli.handle_port_forward(args, store, DummyRuntime())
    assert rc == 0
    assert calls == ["stale-token", "sess1.labs-pf.sig"]


def test_handle_exec_labs_fallback_disabled(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setenv("AE_CLI_LABS_MINT_FALLBACK", "0")
    monkeypatch.setenv("AE_LABS_TOKEN", "labs-token")
    monkeypatch.delenv("AE_APISHIM_MINT_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_EXEC_TOKEN", raising=False)
    monkeypatch.delenv("AE_APISHIM_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(cli, "_resolve_apishim_stream_token", lambda **_k: "stale-token")
    monkeypatch.setattr(
        cli,
        "_exec_over_ws",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("websocket upgrade failed: 401")),
    )
    monkeypatch.setattr(
        cli,
        "_exec_over_spdy",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("spdy upgrade failed: 401")),
    )
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 1


def test_handle_exec_connection_refused_prints_apishim_hint(monkeypatch, tmp_path, capsys):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="https://127.0.0.1:8445",
        stdin=False,
        tty=False,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    monkeypatch.setattr(cli, "_resolve_apishim_stream_token", lambda **_k: "exec-token")
    monkeypatch.setattr(
        cli,
        "_exec_over_ws",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("[Errno 111] Connection refused")),
    )
    monkeypatch.setattr(
        cli,
        "_exec_over_spdy",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("[Errno 111] Connection refused")),
    )
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 1
    out = capsys.readouterr().out
    assert "spdy exec failed:" in out
    assert "apishim appears unreachable" in out
    assert "state/apishim.pid" in out


def test_handle_exec_interactive_prefers_spdy(monkeypatch, tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    args = argparse.Namespace(
        name="echo",
        cmd=["--", "sh"],
        apishim="http://127.0.0.1:8445",
        stdin=True,
        tty=True,
        container=None,
        ws_fallback=False,
        timeout=None,
    )
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None)
    )
    called = {"spdy": False, "ws": False}

    def _fake_spdy(*_a, **_k):
        called["spdy"] = True
        return 0

    def _fake_ws(*_a, **_k):
        called["ws"] = True
        return 0

    monkeypatch.setattr(cli, "_exec_over_spdy", _fake_spdy)
    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    assert called == {"spdy": True, "ws": False}
