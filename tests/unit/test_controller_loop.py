"""Controller daemon one-shot reconcile smoke test."""

from pathlib import Path
from types import SimpleNamespace

from ae.controller.__main__ import main
from ae.controller.spec import AppManifest, AppSpec, Metadata
from ae.controller.state import SQLiteStateStore


def write_manifest(path: Path) -> None:
    write_named_manifest(path, "echo")


def write_named_manifest(path: Path, name: str) -> None:
    path.write_text(
        f"""
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: {name}
spec:
  image: alpine:3.20
  replicas: 1
        """.strip()
    )


def build_manifest(name: str) -> AppManifest:
    return AppManifest(
        apiVersion="ae.dev/v1alpha1",
        kind="Deployment",
        metadata=Metadata(name=name),
        spec=AppSpec(image="alpine:3.20", replicas=1),
    )


class _FakeAuthority:
    def __init__(self, *, is_leader: bool) -> None:
        self._is_leader = is_leader
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def wait_until_ready(self, timeout=None) -> bool:
        return True

    def snapshot(self):
        return SimpleNamespace(is_leader=self._is_leader)

    def stop(self) -> None:
        self.stopped = True


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


def test_controller_once_ha_standby_skips_specs_import(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_manifest(specs_dir / "echo.yaml")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    authority = _FakeAuthority(is_leader=False)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    assert store.list_registered_apps() == []
    assert store.list_status() == []
    assert authority.started is True
    assert authority.stopped is True


def test_controller_once_ha_leader_uses_shared_registry_only(tmp_path, monkeypatch):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    write_named_manifest(specs_dir / "local.yaml", "local-only")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("AE_STATE_DB", str(db_path))
    monkeypatch.setenv("AE_RUNTIME_BACKEND", "stub")
    monkeypatch.setenv("AE_CADDY_SITES", "")
    monkeypatch.setenv("AE_HA_MODE", "1")
    authority = _FakeAuthority(is_leader=True)
    monkeypatch.setattr(
        "ae.controller.__main__.ControllerAuthorityService.from_env",
        lambda: authority,
    )

    store = SQLiteStateStore(db_path)
    store.register_app(build_manifest("persisted"), source="test", labels={})

    assert main(["--once", "--specs", str(specs_dir)]) == 0

    store = SQLiteStateStore(db_path)
    statuses = store.list_status()
    assert any(s.app_name == "persisted" and s.ready_replicas == 1 for s in statuses)
    assert not any(s.app_name == "local-only" for s in statuses)
    assert store.list_registered_app_names() == ["persisted"]
    assert authority.started is True
    assert authority.stopped is True
