"""Controller daemon one-shot reconcile smoke test."""

from pathlib import Path

from ae.controller.__main__ import main
from ae.controller.state import SQLiteStateStore


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


def test_controller_once_reconciles(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_manifest(specs_dir / "echo.yaml")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    # Run once
    assert main(["--once", "--specs", str(specs_dir)]) == 0

    # Verify status persisted
    store = SQLiteStateStore(db_path)
    statuses = store.list_status()
    assert any(s.app_name == "echo" and s.ready_replicas == 1 for s in statuses)
