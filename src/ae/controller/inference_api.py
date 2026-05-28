"""Controller HTTP helpers for InferenceCell resources."""

from __future__ import annotations

from typing import Any

from ae.controller.inference_cell import InferenceCellController, InferenceCellSetController
from ae.controller.spec import (
    AppManifest,
    InferenceCellManifest,
    InferenceCellSetManifest,
    parse_manifest_document,
)
from ae.controller.state import InferenceCellRecord, InferenceCellSetRecord


def apply_manifest_payload(
    store: Any,
    payload: dict[str, Any],
    *,
    source: str = "api",
    authority: Any | None = None,
) -> dict[str, Any]:
    """Apply an inference manifest payload and return controller API status."""

    manifest = parse_manifest_document(payload, source="api payload")
    if isinstance(manifest, AppManifest):
        raise TypeError("expected InferenceCell or InferenceCellSet manifest")
    if isinstance(manifest, InferenceCellManifest):
        controller = InferenceCellController(store, authority=authority)
        return cell_record_payload(controller.reconcile_manifest(manifest, source=source))
    if isinstance(manifest, InferenceCellSetManifest):
        cell_controller = InferenceCellController(store, authority=authority)
        set_controller = InferenceCellSetController(
            store,
            cell_controller=cell_controller,
            authority=authority,
        )
        return cellset_record_payload(set_controller.reconcile_manifest(manifest, source=source))
    raise TypeError(f"unsupported inference manifest type {type(manifest).__name__}")


def delete_resource(
    store: Any,
    kind: str,
    name: str,
    *,
    namespace: str | None = None,
    authority: Any | None = None,
) -> dict[str, Any]:
    """Delete an inference cell or cellset and return a stable API payload."""

    normalized = kind.strip().lower().replace("_", "-")
    if normalized in {"cell", "cells", "inferencecell", "inference-cell"}:
        before = store.get_inference_cell(name, namespace=namespace)
        InferenceCellController(store, authority=authority).delete_cell(name, namespace=namespace)
        return {
            "kind": "InferenceCell",
            "name": name,
            "namespace": namespace or "default",
            "removed": before is not None,
        }
    if normalized in {"cellset", "cellsets", "inferencecellset", "inference-cell-set"}:
        before = store.get_inference_cellset(name, namespace=namespace)
        InferenceCellSetController(store, authority=authority).delete_cellset(
            name,
            namespace=namespace,
        )
        return {
            "kind": "InferenceCellSet",
            "name": name,
            "namespace": namespace or "default",
            "removed": before is not None,
        }
    raise ValueError(f"unsupported inference resource kind {kind!r}")


def cell_record_payload(rec: InferenceCellRecord) -> dict[str, Any]:
    """Serialize an InferenceCellRecord for the controller-native API."""

    allocations = dict(rec.allocations or {})
    phase = str(rec.phase or "PENDING")
    return {
        "kind": "InferenceCell",
        "cell_key": rec.cell_key,
        "cell_id": rec.cell_id,
        "name": rec.cell_id,
        "namespace": rec.namespace,
        "phase": phase,
        "status": phase.lower(),
        "ready": phase.upper() == "READY",
        "model_id": rec.manifest.spec.model.model_id,
        "tp": rec.tp,
        "pp": rec.pp,
        "executor_type": rec.executor_type,
        "active_executor": str(allocations.get("active_executor") or rec.executor_type or ""),
        "api_endpoint": str(allocations.get("api_endpoint") or ""),
        "allocations": allocations,
        "admission": dict(rec.admission or {}),
        "conditions": dict(rec.conditions or {}),
        "restarts": rec.restarts,
        "last_error": rec.last_error,
        "source": rec.source,
        "updated_at": rec.updated_at.isoformat(),
        "manifest": rec.manifest.model_dump(by_alias=True, exclude_none=True),
    }


def cellset_record_payload(rec: InferenceCellSetRecord) -> dict[str, Any]:
    """Serialize an InferenceCellSetRecord for the controller-native API."""

    phase = "READY" if rec.ready >= rec.desired else "PROGRESSING"
    if rec.last_error:
        phase = "FAILED"
    return {
        "kind": "InferenceCellSet",
        "set_key": rec.set_key,
        "name": rec.name,
        "namespace": rec.namespace,
        "phase": phase,
        "status": phase.lower(),
        "ready": rec.ready,
        "desired": rec.desired,
        "current": rec.current,
        "last_error": rec.last_error,
        "source": rec.source,
        "updated_at": rec.updated_at.isoformat(),
        "manifest": rec.manifest.model_dump(by_alias=True, exclude_none=True),
    }
