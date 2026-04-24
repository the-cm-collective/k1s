import pytest

from ae.controller.inference_cell import (
    InferenceCellController,
    InferenceCellSetController,
    StagePlacement,
)
from ae.controller.spec import InferenceCellManifest, InferenceCellSetManifest
from ae.controller.state import SQLiteStateStore


def _cell_manifest(name: str = "demo-cell") -> InferenceCellManifest:
    return InferenceCellManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCell",
            "metadata": {"name": name, "namespace": "default"},
            "spec": {
                "model": {"modelId": "llama", "localPath": "/models/llama"},
                "parallelism": {"tp": 1, "pp": 2},
                "members": [
                    {"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1},
                    {"siteId": "site-b", "nodeId": "node-b", "gpuCount": 1},
                ],
                "linkMetrics": [
                    {
                        "fromSite": "site-a",
                        "toSite": "site-b",
                        "rttP95Ms": 5.0,
                        "jitterP95Ms": 0.2,
                        "lossPct": 0.0,
                    }
                ],
            },
        }
    )


def test_inference_cell_reconcile_ready(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    rec = ctrl.reconcile_manifest(_cell_manifest(), source="test")
    assert rec.phase == "READY"
    assert rec.allocations.get("fabric_session_id")
    sessions = store.list_fabric_sessions(cell_name="demo-cell", namespace="default")
    assert len(sessions) == 1
    events = store.list_inference_cell_events("demo-cell", namespace="default")
    assert any(ev.event_type == "CellReady" for ev in events)


def test_inference_cell_admission_failure_without_metrics(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    manifest = _cell_manifest(name="bad-cell")
    payload = manifest.model_dump(by_alias=True)
    payload["spec"]["linkMetrics"] = []
    bad = InferenceCellManifest.model_validate(payload)
    rec = ctrl.reconcile_manifest(bad, source="test")
    assert rec.phase == "FAILED"
    assert rec.last_error == "ADMISSION_REJECTED"


def test_inference_cellset_scale_to_zero(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    set_ctrl = InferenceCellSetController(store)
    cellset = InferenceCellSetManifest.model_validate(
        {
            "apiVersion": "ae.dev/v1alpha1",
            "kind": "InferenceCellSet",
            "metadata": {"name": "set-a", "namespace": "default"},
            "spec": {
                "replicas": 1,
                "template": _cell_manifest().spec.model_dump(by_alias=True),
            },
        }
    )
    rec = set_ctrl.reconcile_manifest(cellset, source="test")
    assert rec.desired == 1
    assert rec.current >= 1
    scaled = set_ctrl.scale("set-a", 0, namespace="default")
    assert scaled is not None
    assert scaled.desired == 0
    cells = [
        c
        for c in store.list_inference_cells(namespace="default")
        if (c.manifest.metadata.labels or {}).get("k1s.cellset") == "set-a"
    ]
    assert cells == []


def test_inference_cell_execution_mode_requires_registered_members(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    rec = ctrl.reconcile_manifest(_cell_manifest(name="exec-cell"), source="test")
    assert rec.phase == "FAILED"
    assert rec.last_error == "ADMISSION_MEMBER_INVALID"


def test_inference_cell_execution_mode_accepts_typed_accelerators(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AE_INFERENCE_EXPERIMENTAL", "1")
    store = SQLiteStateStore(tmp_path / "state.db")
    for node_id, site_id in (("node-a", "site-a"), ("node-b", "site-b")):
        store.upsert_node(
            node_id,
            name=node_id,
            labels={"site": site_id},
            capabilities={
                "accelerators": [
                    {
                        "id": f"{node_id}-gpu-0",
                        "kind": "discrete_gpu",
                        "vendor": "nvidia",
                        "family": "RTX 8000",
                        "device_count": 1,
                        "memory_model": "dedicated",
                        "memory_bytes_per_device": 49152 * 1024 * 1024,
                        "runtime_handlers": ["nvidia"],
                        "partitioning_mode": "none",
                        "backing_device_id": None,
                        "execution_role": "execution",
                    }
                ]
            },
            endpoint=f"http://{node_id}.lan:9109",
        )
        store.record_heartbeat(node_id, "Ready")

    ctrl = InferenceCellController(store)
    errors = ctrl._validate_members_for_execution(_cell_manifest(name="typed-exec-cell").spec)
    assert errors == []


def test_inference_stage_manifest_sets_runtime_class(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    ctrl = InferenceCellController(store)
    payload = _cell_manifest().model_dump(by_alias=True)
    payload["spec"]["executor"]["runtimeClassName"] = "nvidia"
    manifest = InferenceCellManifest.model_validate(payload)

    stage0 = StagePlacement(stage=0, site_id="site-a", node_id="node-a", gpu_indices=[0])
    alloc = {
        "master_addr": "10.255.0.10",
        "master_port": 22000,
        "api_port": 18080,
    }
    mp_manifest = ctrl._mp_stage_manifest(manifest, stage0, alloc)
    assert mp_manifest.spec.runtime_class_name == "nvidia"
