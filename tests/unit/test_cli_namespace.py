from pathlib import Path

from ae.cli import __main__ as cli
from ae.controller.state import SQLiteStateStore


class DummyRuntime:
    pass


def _write_manifest(path: Path, namespace: str | None = None) -> None:
    ns_block = f"  namespace: {namespace}\n" if namespace else ""
    path.write_text(
        "\n".join(
            [
                "apiVersion: ae.dev/v1alpha1",
                "kind: Deployment",
                "metadata:",
                "  name: echo",
                ns_block.rstrip("\n"),
                "spec:",
                "  image: alpine:3.20",
                "  replicas: 1",
                "",
            ]
        ).strip()
    )


def _setup_exec(monkeypatch, tmp_path, args):
    store = SQLiteStateStore(tmp_path / "state.db")
    monkeypatch.setenv("AE_APISHIM_SERVER", "http://127.0.0.1:8445")
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("green-rev1-0", None)
    )
    captured = {}

    def _fake_spdy(base, namespace, pod_name, command, **_kwargs):
        captured["base"] = base
        captured["namespace"] = namespace
        captured["pod_name"] = pod_name
        captured["command"] = command
        return 0

    monkeypatch.setattr(cli, "_exec_over_spdy", _fake_spdy)
    rc = cli.handle_exec(args, store, DummyRuntime())
    assert rc == 0
    return captured


def test_exec_namespace_flag_after_name(monkeypatch, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["exec", "green", "-n", "demo", "--", "sh"])
    captured = _setup_exec(monkeypatch, tmp_path, args)
    assert captured["namespace"] == "demo"
    assert captured["command"] == ["sh"]


def test_shell_defaults_to_bash(monkeypatch, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["shell", "green", "-n", "demo"])
    store = SQLiteStateStore(tmp_path / "state.db")
    monkeypatch.setenv("AE_APISHIM_SERVER", "http://127.0.0.1:8445")
    monkeypatch.setattr(
        cli, "_resolve_exec_target", lambda _store, _app, _container: ("green-rev1-0", None)
    )
    captured = {}

    def _fake_spdy(_base, namespace, pod_name, command, **_kwargs):
        _ = pod_name
        captured["namespace"] = namespace
        captured["command"] = command
        return 0

    monkeypatch.setattr(cli, "_exec_over_spdy", _fake_spdy)
    rc = cli.handle_shell(args, store, DummyRuntime())
    assert rc == 0
    assert captured["namespace"] == "demo"
    assert captured["command"] == ["bash"]


def test_apply_namespace_override(monkeypatch, tmp_path, capsys):
    manifest_path = tmp_path / "echo.yaml"
    _write_manifest(manifest_path)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    registry_config = tmp_path / "registries.yaml"
    registry_config.write_text(
        "\n".join(
            [
                "ghcr.io:",
                "  username: demo",
                "  password: token",
                "",
            ]
        ).strip()
    )
    monkeypatch.setenv("AE_REGISTRY_CONFIG", str(registry_config))

    exit_code = cli.main(["apply", "-n", "demo", "-f", str(manifest_path)])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Applied demo/echo" in output


def test_apply_force_namespace_overrides_manifest(monkeypatch, tmp_path, capsys):
    manifest_path = tmp_path / "echo.yaml"
    _write_manifest(manifest_path, namespace="prod")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    registry_config = tmp_path / "registries.yaml"
    registry_config.write_text(
        "\n".join(
            [
                "ghcr.io:",
                "  username: demo",
                "  password: token",
                "",
            ]
        ).strip()
    )
    monkeypatch.setenv("AE_REGISTRY_CONFIG", str(registry_config))

    exit_code = cli.main(["apply", "-n", "demo", "--force-namespace", "-f", str(manifest_path)])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Applied demo/echo" in output
