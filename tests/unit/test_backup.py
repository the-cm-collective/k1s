"""Backup and restore CLI tests."""

from __future__ import annotations

import tarfile
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


def test_backup_and_restore(tmp_path, monkeypatch, capsys):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_manifest(specs_dir / "echo.yaml")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_SPECS_DIR", str(specs_dir))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")

    # Run apply once to create the DB
    assert main(["apply", "-f", str(specs_dir / "echo.yaml")]) == 0
    capsys.readouterr()

    out_tar = tmp_path / "backup.tar.gz"
    assert main(["backup", "create", "--output", str(out_tar)]) == 0
    text = capsys.readouterr().out
    assert "backup written" in text

    # Inspect tar contains expected paths
    with tarfile.open(out_tar, "r:gz") as tar:
        names = tar.getnames()
    assert "state/controller.db" in names
    assert any(n.startswith("specs/") for n in names)

    # Restore into a new directory
    target = tmp_path / "restore"
    assert main(["backup", "restore", "--input", str(out_tar), "--into", str(target)]) == 0
    text = capsys.readouterr().out
    assert "backup restored into" in text
    assert (target / "state" / "controller.db").exists()
    assert (target / "specs").exists()

    # List contents
    assert main(["backup", "list", "--input", str(out_tar)]) == 0
    listed = capsys.readouterr().out
    assert "state/controller.db" in listed

    # Verify
    assert main(["backup", "verify", "--input", str(out_tar)]) == 0
    verify_out = capsys.readouterr().out
    assert "verify: ok" in verify_out
