import argparse
import time

from ae.cli import __main__ as cli
from ae.controller.state import SQLiteStateStore


class DummyRuntime:
    pass


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
