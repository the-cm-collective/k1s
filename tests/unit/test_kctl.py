"""k1s (kubectl-like) CLI smoke tests."""

from pathlib import Path

import ae.kctl.__main__ as kctl_main
from ae.kctl.__main__ import build_parser, main
from ae.kctl.lifecycle import LifecycleResult


def write_manifest(path: Path, image: str = "alpine:3.20") -> None:
    path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: echo
spec:
  image: {image}
  replicas: 1
        """.strip()
    )


def test_get_describe_rollout_and_logs(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "echo.yaml"
    write_manifest(manifest)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "applied default/echo" in out

    # get apps
    assert main(["get", "apps"]) == 0
    out = capsys.readouterr().out
    assert "default/echo" in out and "ready=1" in out

    # get single app
    assert main(["get", "app", "echo"]) == 0
    out = capsys.readouterr().out
    assert "default/echo: desired=1" in out

    # describe
    assert main(["describe", "app/echo"]) == 0
    out = capsys.readouterr().out
    assert "event" in out
    assert "pod" in out or "- echo-rev" in out

    # rollout history
    assert main(["rollout", "history", "echo", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "rev 1" in out

    # logs
    assert main(["logs", "app/echo"]) == 0
    out = capsys.readouterr().out
    assert "echo-rev1-0" in out


def test_rollout_undo(tmp_path, monkeypatch, capsys):
    v1 = tmp_path / "echo.yaml"
    write_manifest(v1, image="alpine:3.20")
    v2 = tmp_path / "echo-v2.yaml"
    write_manifest(v2, image="alpine:3.21")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    assert main(["apply", "-f", str(v1)]) == 0
    capsys.readouterr()
    assert main(["apply", "-f", str(v2)]) == 0
    capsys.readouterr()

    assert main(["rollout", "undo", "app/echo", "--to-revision", "1"]) == 0
    out = capsys.readouterr().out
    assert "rolled back default/echo" in out


def test_profile_lifecycle_cli_does_not_initialize_workload_runtime(monkeypatch, capsys):
    calls: list[tuple[str, str]] = []

    class FakeManager:
        def perform(self, action: str, profile: str) -> LifecycleResult:
            calls.append((action, profile))
            return LifecycleResult(
                action="start",
                profile="core-edge",
                status="started",
                exit_code=0,
                pid=1234,
                command=("/repo/scripts/dev/run_profile.sh", "k1s-core"),
            )

    monkeypatch.setattr(kctl_main, "profile_lifecycle_manager_factory", FakeManager)
    monkeypatch.setattr(
        kctl_main,
        "_setup",
        lambda: (_ for _ in ()).throw(AssertionError("workload runtime initialized")),
    )

    assert main(["profile", "start", "core-edge", "--output", "json"]) == 0
    assert calls == [("start", "core-edge")]
    assert '"profile": "core-edge"' in capsys.readouterr().out


def test_k1s_help_preserves_workload_commands_and_adds_profile_lifecycle() -> None:
    help_text = build_parser().format_help()
    for command in ("get", "describe", "apply", "rollout", "logs", "events", "delete", "scale"):
        assert command in help_text
    assert "profile" in help_text
