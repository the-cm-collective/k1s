import argparse

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
    monkeypatch.setattr(cli, "_resolve_exec_target", lambda _store, _app, _container: ("echo-rev1-0", None))
    monkeypatch.setattr(cli, "_exec_over_spdy", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("spdy")))
    called = {"ws": False}

    def _fake_ws(*_a, **_k):
        called["ws"] = True
        return 0

    monkeypatch.setattr(cli, "_exec_over_ws", _fake_ws)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    assert called["ws"] is True
