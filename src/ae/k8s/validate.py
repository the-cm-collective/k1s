"""Lightweight structural validator for exported K8s YAML."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import yaml


def validate_documents(yaml_text: str) -> Tuple[bool, List[str]]:
    """Validate a multi-doc YAML string for basic schema sanity.

    Checks:
    - Each doc has apiVersion, kind, metadata.name
    - Known stable api groups for the kinds we emit
    - Kind-specific required fields (minimal subset)
    """

    errors: List[str] = []
    try:
        docs = list(yaml.safe_load_all(yaml_text))
    except yaml.YAMLError as exc:
        return False, [f"YAML parse error: {exc}"]

    for i, doc in enumerate(docs, start=1):
        if not isinstance(doc, dict):
            errors.append(f"doc {i}: not a mapping")
            continue
        api = str(doc.get("apiVersion", ""))
        kind = str(doc.get("kind", ""))
        meta = doc.get("metadata") or {}
        name = (meta or {}).get("name")
        if not api or not kind or not name:
            errors.append(f"doc {i}: missing apiVersion/kind/metadata.name")
            continue
        # Stable api checks
        if kind in {"Deployment"} and api != "apps/v1":
            errors.append(f"doc {i}: Deployment must use apps/v1")
        if (
            kind in {"Service", "PersistentVolumeClaim", "Secret", "ConfigMap", "ServiceAccount"}
            and api != "v1"
        ):
            errors.append(f"doc {i}: {kind} must use v1")
        if kind == "Ingress" and api != "networking.k8s.io/v1":
            errors.append(f"doc {i}: Ingress must use networking.k8s.io/v1")
        if kind == "PodDisruptionBudget" and api != "policy/v1":
            errors.append(f"doc {i}: PDB must use policy/v1")
        if kind == "HorizontalPodAutoscaler" and api != "autoscaling/v2":
            errors.append(f"doc {i}: HPA must use autoscaling/v2")

        # Minimal kind-specific structure
        if kind == "Deployment":
            spec = doc.get("spec", {})
            tmpl = (spec.get("template") or {}).get("spec") or {}
            containers = tmpl.get("containers") or []
            if not containers:
                errors.append(f"doc {i}: Deployment spec.template.spec.containers required")
        if kind == "Service":
            s = doc.get("spec", {})
            ports = s.get("ports") or []
            if not ports:
                errors.append(f"doc {i}: Service.spec.ports required")
        if kind == "Ingress":
            s = doc.get("spec", {})
            rules = s.get("rules") or []
            if not rules:
                errors.append(f"doc {i}: Ingress.spec.rules required")

    return (len(errors) == 0), errors
# ruff: noqa
# ruff: noqa: E501,UP006,UP007,UP017,UP035,F401
