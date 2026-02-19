"""Helpers for normalizing edge ingress route/policy documents."""

from __future__ import annotations

from typing import Any

from ae.controller.state import EdgeIngressPolicyRecord, EdgeIngressRouteRecord


def _ensure_metadata(meta: Any, *, name: str, namespace: str) -> dict:
    out: dict = dict(meta) if isinstance(meta, dict) else {}
    if name and not out.get("name"):
        out["name"] = name
    if namespace and not out.get("namespace"):
        out["namespace"] = namespace
    return out


def normalize_route_doc(record: EdgeIngressRouteRecord) -> dict:
    """Return a full EdgeIngressRoute document with metadata/spec keys."""
    doc = record.spec if isinstance(record.spec, dict) else {}
    if "spec" in doc:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        meta = _ensure_metadata(doc.get("metadata"), name=record.name, namespace=record.namespace)
        out = dict(doc)
        out.setdefault("apiVersion", "k1s.io/v1")
        out.setdefault("kind", "EdgeIngressRoute")
        out["metadata"] = meta
        out["spec"] = spec
        return out
    meta = _ensure_metadata({}, name=record.name, namespace=record.namespace)
    return {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": meta,
        "spec": doc if isinstance(doc, dict) else {},
    }


def normalize_policy_doc(record: EdgeIngressPolicyRecord) -> dict:
    """Return a full EdgeIngressPolicy document with metadata/spec keys."""
    doc = record.spec if isinstance(record.spec, dict) else {}
    if "spec" in doc:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        meta = _ensure_metadata(doc.get("metadata"), name=record.name, namespace=record.namespace)
        out = dict(doc)
        out.setdefault("apiVersion", "k1s.io/v1")
        out.setdefault("kind", "EdgeIngressPolicy")
        out["metadata"] = meta
        out["spec"] = spec
        return out
    meta = _ensure_metadata({}, name=record.name, namespace=record.namespace)
    return {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressPolicy",
        "metadata": meta,
        "spec": doc if isinstance(doc, dict) else {},
    }


__all__ = ["normalize_route_doc", "normalize_policy_doc"]
