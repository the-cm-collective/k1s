"""CLI integration smoke tests."""

from pathlib import Path

from ae.cli.__main__ import main


def write_manifest(path: Path) -> None:
    path.write_text(
        """
apiVersion: ae.dev/v1alpha1
kind: App
metadata:
  name: echo
spec:
  image: alpine:3.20
  replicas: 1
        """.strip()
    )


def test_apply_and_status_commands(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "echo.yaml"
    write_manifest(manifest_path)

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")

    exit_code = main(["apply", "-f", str(manifest_path)])
    assert exit_code == 0
    apply_out = capsys.readouterr().out
    assert "Applied echo" in apply_out

    exit_code = main(["status", "echo"])
    assert exit_code == 0
    status_out = capsys.readouterr().out
    assert "desired=1" in status_out
    assert "ready=1" in status_out
    assert "live=1" in status_out
    assert "ops=+1" in status_out
    assert "  - echo-0" in status_out

    exit_code = main(["status"])
    assert exit_code == 0
    list_out = capsys.readouterr().out
    assert "echo" in list_out
    assert "live=1" in list_out
    assert "ops=+1" in list_out

    exit_code = main(["status", "echo", "--history", "3"])
    assert exit_code == 0
    history_out = capsys.readouterr().out
    assert "history" in history_out
    assert "readiness" in history_out


def test_logs_command(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")

    exit_code = main(["logs", "ghost"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Logs for ghost" in output
