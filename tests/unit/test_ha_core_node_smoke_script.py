from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

from ae.controller.state import SQLiteStateStore


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ha_core_node_smoke.py"

_SPEC = spec_from_file_location("ha_core_node_smoke_script", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ha_core_node_smoke = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ha_core_node_smoke
_SPEC.loader.exec_module(ha_core_node_smoke)


def test_find_ready_node_accepts_expected_labels(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    store.upsert_node("hub-1", name="hub-1", labels={"role": "hub", "site": "hub"})
    store.record_heartbeat("hub-1", "ready")

    ok, detail = ha_core_node_smoke.find_ready_node(
        store,
        "hub-1",
        {"role": "hub", "site": "hub"},
    )

    assert ok is True
    assert detail == "node ready: hub-1"


def test_find_ready_node_rejects_label_mismatch(tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    store.upsert_node("hub-1", name="hub-1", labels={"role": "hub", "site": "lab"})
    store.record_heartbeat("hub-1", "ready")

    ok, detail = ha_core_node_smoke.find_ready_node(
        store,
        "hub-1",
        {"role": "hub", "site": "hub"},
    )

    assert ok is False
    assert detail == "node labels mismatch: site=hub"


def test_load_smoke_manifest_overrides_name() -> None:
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        "ha-core-node-smoke",
    )

    assert manifest.metadata.name == "ha-core-node-smoke"
    assert manifest.spec.image == "docker.io/library/demo-shell:latest"
    assert manifest.spec.node_selector == {"role": "hub", "site": "hub"}


def test_run_workload_smoke_cleans_up_registered_state(monkeypatch, tmp_path: Path) -> None:
    store = SQLiteStateStore(tmp_path / "controller.db")
    manifest = ha_core_node_smoke.load_smoke_manifest(
        ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        "ha-core-node-smoke",
    )
    app_name = manifest.metadata.name
    cleanup_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(ha_core_node_smoke, "state_store_from_env", lambda: store)
    monkeypatch.setattr(ha_core_node_smoke, "wait_for_node_ready", lambda *args, **kwargs: "node ready: hub-1")
    monkeypatch.setattr(ha_core_node_smoke, "load_smoke_manifest", lambda path, name: manifest)
    monkeypatch.setattr(
        ha_core_node_smoke,
        "wait_for_workload_ready",
        lambda *args, **kwargs: "workload ready: app=ha-core-node-smoke desired=1 ready=1 live=1",
    )

    original_cleanup = ha_core_node_smoke.cleanup_workload

    def _wrapped_cleanup(store_obj, app_key: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
        cleanup_calls.append((app_key, purge_history))
        original_cleanup(
            store_obj,
            app_key,
            timeout_s=timeout_s,
            poll_s=poll_s,
            purge_history=purge_history,
        )

    monkeypatch.setattr(ha_core_node_smoke, "cleanup_workload", _wrapped_cleanup)

    args = SimpleNamespace(
        node_id="hub-1",
        label=["role=hub", "site=hub"],
        manifest=ROOT / "docs" / "site" / "examples" / "shell-demo-node-hub.yaml",
        app_name=app_name,
        timeout=5,
        poll=0.01,
        purge_history=True,
    )

    rc = ha_core_node_smoke.run_workload_smoke(args)

    assert rc == 0
    assert store.get_registered_entry(app_name) is None
    assert cleanup_calls == [(app_name, True)]
