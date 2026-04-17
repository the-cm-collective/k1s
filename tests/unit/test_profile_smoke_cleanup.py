from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest

from tests.integration import _profile_smoke as profile_smoke


def test_remove_tree_with_retries_removes_existing_tree(tmp_path: Path) -> None:
    root = tmp_path / "compose-state"
    (root / "etcd").mkdir(parents=True)
    (root / "etcd" / "member").write_text("ok", encoding="utf-8")

    profile_smoke.remove_tree_with_retries(root, timeout_s=0.1, interval_s=0.01)

    assert not root.exists()


def test_remove_tree_with_retries_retries_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "compose-state"
    root.mkdir()
    attempts = {"count": 0}
    original_rmtree = shutil.rmtree

    def flaky_rmtree(path: Path) -> None:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        original_rmtree(path)

    monkeypatch.setattr(profile_smoke.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(profile_smoke.time, "sleep", lambda _seconds: None)

    profile_smoke.remove_tree_with_retries(root, timeout_s=1.0, interval_s=0.01)

    assert attempts["count"] == 3
    assert not root.exists()


def test_remove_tree_with_retries_raises_with_tree_snapshot(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "compose-state"
    child = root / "postgres"
    child.mkdir(parents=True)
    (child / "PG_VERSION").write_text("16", encoding="utf-8")

    def always_busy(_path: Path) -> None:
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    ticks = iter([0.0, 0.6, 1.2])
    monkeypatch.setattr(profile_smoke.shutil, "rmtree", always_busy)
    monkeypatch.setattr(profile_smoke.time, "time", lambda: next(ticks))
    monkeypatch.setattr(profile_smoke.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="failed to remove tree"):
        profile_smoke.remove_tree_with_retries(root, timeout_s=1.0, interval_s=0.01)
