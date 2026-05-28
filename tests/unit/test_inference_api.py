from __future__ import annotations

from ae.controller.inference_api import (
    apply_manifest_payload,
    cell_record_payload,
    cellset_record_payload,
    delete_resource,
)
from ae.controller.spec import InferenceCellManifest, InferenceCellSetManifest
from ae.controller.state import SQLiteStateStore


def _cell_payload(name: str = "demo-cell") -> dict:
    return {
        "apiVersion": "ae.dev/v1alpha1",
        "kind": "InferenceCell",
        "metadata": {"name": name, "namespace": "ml"},
        "spec": {
            "model": {"modelId": "llama", "localPath": "/models/llama"},
            "parallelism": {"tp": 1, "pp": 1},
            "members": [{"siteId": "site-a", "nodeId": "node-a", "gpuCount": 1}],
        },
    }


def _cellset_payload(name: str = "demo-set") -> dict:
    return {
        "apiVersion": "ae.dev/v1alpha1",
        "kind": "InferenceCellSet",
        "metadata": {"name": name, "namespace": "ml"},
        "spec": {
            "replicas": 2,
            "template": _cell_payload("template")["spec"],
        },
    }


def test_apply_inference_cell_payload_returns_discovery_shape(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    payload = apply_manifest_payload(store, _cell_payload(), source="test")

    assert payload["kind"] == "InferenceCell"
    assert payload["name"] == "demo-cell"
    assert payload["namespace"] == "ml"
    assert payload["phase"] == "READY"
    assert payload["ready"] is True
    assert payload["model_id"] == "llama"
    stored = store.get_inference_cell("demo-cell", namespace="ml")
    assert stored is not None
    assert cell_record_payload(stored)["status"] == "ready"


def test_apply_inference_cellset_payload_and_delete(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    payload = apply_manifest_payload(store, _cellset_payload(), source="test")

    assert payload["kind"] == "InferenceCellSet"
    assert payload["desired"] == 2
    assert payload["current"] == 2
    stored = store.get_inference_cellset("demo-set", namespace="ml")
    assert stored is not None
    assert cellset_record_payload(stored)["status"] in {"ready", "progressing"}
    cells = [
        cell
        for cell in store.list_inference_cells(namespace="ml")
        if (cell.manifest.metadata.labels or {}).get("k1s.cellset") == "demo-set"
    ]
    assert len(cells) == 2

    result = delete_resource(store, "cellsets", "demo-set", namespace="ml")

    assert result["removed"] is True
    assert store.get_inference_cellset("demo-set", namespace="ml") is None
    assert [
        cell
        for cell in store.list_inference_cells(namespace="ml")
        if (cell.manifest.metadata.labels or {}).get("k1s.cellset") == "demo-set"
    ] == []


def test_serializers_accept_existing_records(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    cell = InferenceCellManifest.model_validate(_cell_payload("shape-cell"))
    cellset = InferenceCellSetManifest.model_validate(_cellset_payload("shape-set"))
    store.register_inference_cell(cell, source="test")
    store.register_inference_cellset(cellset, source="test")

    cell_rec = store.get_inference_cell("shape-cell", namespace="ml")
    set_rec = store.get_inference_cellset("shape-set", namespace="ml")

    assert cell_rec is not None
    assert set_rec is not None
    assert cell_record_payload(cell_rec)["manifest"]["kind"] == "InferenceCell"
    assert cellset_record_payload(set_rec)["manifest"]["kind"] == "InferenceCellSet"
