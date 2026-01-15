# ruff: noqa: E501,S105,S110,S112,SIM102,SIM105,SIM108,SIM114,SIM118,SIM300
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import json as _jsonlib
import os
import re
import secrets
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ae.controller.state import AppEvent, ServiceEndpoint, SQLiteStateStore
from ae.runtime import DockerRuntime, PodmanRuntime, RemoteRuntime, RuntimeAdapter, StubRuntime

from .adapter import build_adapter
from .store import K8sObject, ObjectStore

K8S_VERSION = {
    # Report a modern-ish Kubernetes version so Helm chooses current API groups
    # (e.g., networking.k8s.io/v1 for Ingress).
    "major": "1",
    "minor": "29",
    "gitVersion": "v1.29.0-k1s-shim",
}

RESERVED_GROUPS = {
    "",
    "apps",
    "networking.k8s.io",
    "rbac.authorization.k8s.io",
    "authorization.k8s.io",
    "policy",
    "autoscaling",
    "apiextensions.k8s.io",
}


def _json(d: dict[str, Any]) -> bytes:
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def _read_json(body: bytes) -> dict[str, Any]:
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return {}


def _ns_name(path: str) -> tuple[str, str | None, str | None]:
    # Returns (resource plural, namespace, name)
    # Patterns we support:
    # /api/v1/namespaces
    # /api/v1/namespaces/<ns>
    # /api/v1/namespaces/<ns>/<plural>
    # /api/v1/namespaces/<ns>/<plural>/<name>
    # /api/v1/<plural>
    m = re.match(r"^/api/v1/namespaces/([^/]+)/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(2), m.group(1), m.group(3))
    m = re.match(r"^/api/v1/namespaces/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(2), m.group(1), None)
    m = re.match(r"^/api/v1/namespaces/([^/]+)$", path)
    if m:
        return ("namespaces", None, m.group(1))
    m = re.match(r"^/api/v1/([^/]+)/([^/]+)$", path)
    if m:
        return (m.group(1), None, m.group(2))
    m = re.match(r"^/api/v1/([^/]+)$", path)
    if m:
        return (m.group(1), None, None)
    return ("", None, None)


def _app_name(ns: str | None, name: str) -> str:
    return f"{ns}--{name}" if ns else name


def _rule_matches(resource: str, resources: list[str] | None) -> bool:
    if not resources:
        return False
    if resource in resources or "*" in resources:
        return True
    # allow subresource match like pods/log against pods/*
    if "/" in resource:
        prefix = resource.split("/")[0] + "/*"
        return prefix in resources
    return False


def _json_pointer_tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("pointer must start with /")
    if path == "/":
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]


def _json_pointer_get(doc: Any, path: str) -> Any:
    ref = doc
    for tok in _json_pointer_tokens(path):
        if isinstance(ref, list):
            idx = int(tok) if tok != "-" else len(ref)
            ref = ref[idx]
        elif isinstance(ref, dict):
            ref = ref[tok]
        else:
            raise KeyError
    return ref


def _json_pointer_set(doc: Any, path: str, value: Any) -> Any:
    if path == "" or path == "/":
        return value
    ref = doc
    tokens = _json_pointer_tokens(path)
    for tok in tokens[:-1]:
        if isinstance(ref, list):
            idx = int(tok) if tok != "-" else len(ref)
            while idx >= len(ref):
                ref.append({})
            if not isinstance(ref[idx], dict | list):
                ref[idx] = {}
            ref = ref[idx]
        else:
            if tok not in ref or not isinstance(ref[tok], dict | list):
                ref[tok] = {}
            ref = ref[tok]
    last = tokens[-1]
    if isinstance(ref, list):
        idx = int(last) if last != "-" else len(ref)
        if idx == len(ref):
            ref.append(value)
        else:
            ref[idx] = value
    else:
        ref[last] = value
    return doc


def _json_pointer_remove(doc: Any, path: str) -> Any:
    tokens = _json_pointer_tokens(path)
    if not tokens:
        return None
    ref = doc
    for tok in tokens[:-1]:
        if isinstance(ref, list):
            ref = ref[int(tok)]
        else:
            ref = ref[tok]
    last = tokens[-1]
    if isinstance(ref, list):
        del ref[int(last)]
    else:
        ref.pop(last, None)
    return doc


def _apply_json_patch(doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = _jsonlib.loads(_jsonlib.dumps(doc))
    try:
        for op in ops:
            action = (op.get("op") or "").lower()
            path = op.get("path") or ""
            if action in {"add", "replace"}:
                target = _json_pointer_set(target, path, op.get("value"))
            elif action == "remove":
                target = _json_pointer_remove(target, path)
            else:
                raise ValueError(f"unsupported op {action}")
        return target
    except Exception:
        return None


def _list_item_key(obj: dict[str, Any]) -> Any:
    for key in ("name", "port", "containerPort", "mountPath", "path"):
        if key in obj:
            return obj.get(key)
    return None


def _extract_field_paths(doc: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(doc, dict):
        for k, v in doc.items():
            path = f"{prefix}/{k}" if prefix else f"/{k}"
            paths.add(path)
            paths |= _extract_field_paths(v, path)
    elif isinstance(doc, list):
        for item in doc:
            if isinstance(item, dict):
                token = _list_item_key(item)
                seg = f"[{token}]" if token is not None else "[]"
                path = f"{prefix}/{seg}" if prefix else f"/{seg}"
                paths.add(path)
                paths |= _extract_field_paths(item, path)
            else:
                path = f"{prefix}/[]" if prefix else "/[]"
                paths.add(path)
    return paths


def _fieldsV1_to_paths(fields: dict[str, Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for k, v in (fields or {}).items():
        if k.startswith("f:"):
            key = k[2:]
            path = f"{prefix}/{key}" if prefix else f"/{key}"
        elif k.startswith("k:"):
            key = k[2:]
            path = f"{prefix}/[{key}]" if prefix else f"/[{key}]"
        else:
            path = f"{prefix}/{k}" if prefix else f"/{k}"
        paths.add(path)
        if isinstance(v, dict):
            paths |= _fieldsV1_to_paths(v, path)
    return paths


def _paths_to_fieldsV1(paths: set[str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path in paths:
        if not path:
            continue
        ref = root
        tokens = [p for p in path.split("/") if p]
        for tok in tokens:
            if tok.startswith("[") and tok.endswith("]"):
                key = f"k:{tok[1:-1]}"
            else:
                key = f"f:{tok}"
            ref = ref.setdefault(key, {})  # type: ignore[assignment]
    return root


def _managed_path_map(md: dict[str, Any]) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for mf in md.get("managedFields") or []:
        mgr = mf.get("manager")
        if not mgr:
            continue
        paths = set(mf.get("paths") or _fieldsV1_to_paths(mf.get("fieldsV1") or {}))
        if not paths and (mf.get("operation") or "").lower() == "apply":
            paths = {"*"}
        mapping[mgr] = paths
    return mapping


def _managed_conflict(md: dict[str, Any], manager: str, new_paths: set[str], force: bool) -> bool:
    if not new_paths:
        return False
    for mgr, paths in _managed_path_map(md).items():
        if mgr == manager or not paths:
            continue
        if "*" in paths:
            if not force:
                return True
            continue
        if new_paths & paths and not force:
            return True
    return False


def _update_managed_fields(
    md: dict[str, Any],
    api_version: str,
    manager: str,
    operation: str = "Apply",
    *,
    fields: set[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    managed = list(md.get("managedFields") or [])
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017 - timezone-aware stamp for managedFields
    new_paths = set(fields or set())
    cleaned: list[dict[str, Any]] = []
    existing_paths: set[str] = set()
    for mf in managed:
        mgr = mf.get("manager")
        if not mgr:
            continue
        paths = set(mf.get("paths") or _fieldsV1_to_paths(mf.get("fieldsV1") or {}))
        if not paths and (mf.get("operation") or "").lower() == "apply":
            paths = {"*"}
        if mgr == manager:
            existing_paths = paths
            continue
        # honor force by stripping overlapping paths
        if force and new_paths:
            if "*" in paths:
                continue
            paths -= new_paths
        if paths:
            mf_out = dict(mf)
            mf_out["paths"] = sorted(paths)
            mf_out["fieldsV1"] = _paths_to_fieldsV1(paths)
            cleaned.append(mf_out)
    combined_paths = existing_paths | new_paths
    entry: dict[str, Any] = {
        "manager": manager,
        "operation": operation,
        "apiVersion": api_version,
        "time": now,
        "fieldsType": "FieldsV1",
        "paths": sorted(combined_paths),
        "fieldsV1": _paths_to_fieldsV1(combined_paths),
    }
    cleaned.append(entry)
    md["managedFields"] = cleaned
    return md


def _inject_sa_projection(spec: dict[str, Any]) -> dict[str, Any]:
    tpl = spec.get("template") or {}
    pod_spec = tpl.get("spec") if tpl else spec.get("podSpec") or spec.get("spec")
    if pod_spec is None:
        # assume pod-level spec already
        pod_spec = spec
    volumes = list(pod_spec.get("volumes") or [])
    mounts_added = False
    vol_name = "k1s-sa-token"
    has_vol = any(v.get("name") == vol_name for v in volumes)
    if not has_vol:
        volumes.append(
            {
                "name": vol_name,
                "projected": {
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "path": "token",
                                "expirationSeconds": 3600,
                                "audience": "apishim",
                            }
                        }
                    ]
                },
            }
        )
        pod_spec["volumes"] = volumes
    containers = pod_spec.get("containers") or []
    for c in containers:
        vms = list(c.get("volumeMounts") or [])
        if not any(vm.get("name") == vol_name for vm in vms):
            vms.append(
                {
                    "name": vol_name,
                    "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                    "readOnly": True,
                }
            )
            c["volumeMounts"] = vms
            mounts_added = True
    if containers and not mounts_added:
        # nothing changed but ensure containers list set back
        pod_spec["containers"] = containers
    pod_spec.setdefault("automountServiceAccountToken", True)
    if tpl:
        tpl["spec"] = pod_spec
        spec["template"] = tpl
    else:
        spec = pod_spec
    return spec


def _swagger_doc() -> dict[str, Any]:
    # Minimal swagger doc for kubectl/helm discovery and --dry-run=server
    schemas = {
        "io.k8s.api.meta.v1.ObjectMeta": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "namespace": {"type": "string"},
                "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                "annotations": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.LoadBalancerIngress": {
            "type": "object",
            "properties": {
                "ip": {"type": "string"},
                "hostname": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.LoadBalancerStatus": {
            "type": "object",
            "properties": {
                "ingress": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.api.core.v1.LoadBalancerIngress"},
                }
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.ServiceStatus": {
            "type": "object",
            "properties": {
                "loadBalancer": {"$ref": "#/definitions/io.k8s.api.core.v1.LoadBalancerStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.ConfigMap": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "data": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.Secret": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "data": {"type": "object"},
                "type": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.ServiceAccount": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "secrets": {"type": "array", "items": {"type": "object"}},
                "imagePullSecrets": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.ServicePort": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "protocol": {"type": "string"},
                "port": {"type": "integer"},
                "targetPort": {"type": ["integer", "string"]},
                "nodePort": {"type": "integer"},
                "appProtocol": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.core.v1.Service": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "clusterIP": {"type": "string"},
                        "clusterIPs": {"type": "array", "items": {"type": "string"}},
                        "ipFamilies": {"type": "array", "items": {"type": "string"}},
                        "ipFamilyPolicy": {"type": "string"},
                        "externalIPs": {"type": "array", "items": {"type": "string"}},
                        "loadBalancerIP": {"type": "string"},
                        "loadBalancerSourceRanges": {"type": "array", "items": {"type": "string"}},
                        "externalTrafficPolicy": {"type": "string"},
                        "sessionAffinity": {"type": "string"},
                        "sessionAffinityConfig": {"type": "object", "additionalProperties": True},
                        "selector": {"type": "object", "additionalProperties": {"type": "string"}},
                        "ports": {
                            "type": "array",
                            "items": {"$ref": "#/definitions/io.k8s.api.core.v1.ServicePort"},
                        },
                    },
                    "additionalProperties": True,
                },
                "status": {"$ref": "#/definitions/io.k8s.api.core.v1.ServiceStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.DeploymentCondition": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "message": {"type": "string"},
                "lastUpdateTime": {"type": "string", "format": "date-time"},
                "lastTransitionTime": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.DeploymentStatus": {
            "type": "object",
            "properties": {
                "replicas": {"type": "integer"},
                "readyReplicas": {"type": "integer"},
                "availableReplicas": {"type": "integer"},
                "unavailableReplicas": {"type": "integer"},
                "updatedReplicas": {"type": "integer"},
                "observedGeneration": {"type": "integer"},
                "conditions": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.api.apps.v1.DeploymentCondition"},
                },
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.Deployment": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.apps.v1.DeploymentStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.StatefulSetCondition": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "message": {"type": "string"},
                "lastTransitionTime": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.StatefulSetStatus": {
            "type": "object",
            "properties": {
                "replicas": {"type": "integer"},
                "readyReplicas": {"type": "integer"},
                "updatedReplicas": {"type": "integer"},
                "currentReplicas": {"type": "integer"},
                "observedGeneration": {"type": "integer"},
                "availableReplicas": {"type": "integer"},
                "currentRevision": {"type": "string"},
                "updateRevision": {"type": "string"},
                "conditions": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.api.apps.v1.StatefulSetCondition"},
                },
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.StatefulSet": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.apps.v1.StatefulSetStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.DaemonSetCondition": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "message": {"type": "string"},
                "lastTransitionTime": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.DaemonSetStatus": {
            "type": "object",
            "properties": {
                "desiredNumberScheduled": {"type": "integer"},
                "currentNumberScheduled": {"type": "integer"},
                "numberAvailable": {"type": "integer"},
                "numberReady": {"type": "integer"},
                "updatedNumberScheduled": {"type": "integer"},
                "observedGeneration": {"type": "integer"},
                "numberMisscheduled": {"type": "integer"},
                "collisionCount": {"type": "integer"},
                "conditions": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.api.apps.v1.DaemonSetCondition"},
                },
            },
            "additionalProperties": True,
        },
        "io.k8s.api.apps.v1.DaemonSet": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.apps.v1.DaemonSetStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.batch.v1.JobStatus": {
            "type": "object",
            "properties": {
                "active": {"type": "integer"},
                "succeeded": {"type": "integer"},
                "failed": {"type": "integer"},
                "startTime": {"type": "string", "format": "date-time"},
                "completionTime": {"type": "string", "format": "date-time"},
                "uncountedTerminatedPods": {"type": "object", "additionalProperties": True},
                "conditions": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.batch.v1.Job": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.batch.v1.JobStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.batch.v1.CronJobStatus": {
            "type": "object",
            "properties": {
                "active": {"type": "array", "items": {"type": "object"}},
                "lastScheduleTime": {"type": "string", "format": "date-time"},
                "lastSuccessfulTime": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.batch.v1.CronJob": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.batch.v1.CronJobStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.autoscaling.v2.MetricStatus": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "resource": {"type": "object", "additionalProperties": True},
                "pods": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.autoscaling.v2.HorizontalPodAutoscalerStatus": {
            "type": "object",
            "properties": {
                "currentReplicas": {"type": "integer"},
                "desiredReplicas": {"type": "integer"},
                "currentMetrics": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.api.autoscaling.v2.MetricStatus"},
                },
                "lastScaleTime": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.autoscaling.v2.HorizontalPodAutoscaler": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.autoscaling.v2.HorizontalPodAutoscalerStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelectorRequirement": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "operator": {"type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelector": {
            "type": "object",
            "properties": {
                "matchLabels": {"type": "object", "additionalProperties": {"type": "string"}},
                "matchExpressions": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelectorRequirement"},
                },
            },
            "additionalProperties": True,
        },
        "io.k8s.api.policy.v1.PodDisruptionBudgetSpec": {
            "type": "object",
            "properties": {
                "minAvailable": {"type": ["integer", "string"]},
                "maxUnavailable": {"type": ["integer", "string"]},
                "selector": {"$ref": "#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelector"},
                "unhealthyPodEvictionPolicy": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.policy.v1.PodDisruptionBudgetStatus": {
            "type": "object",
            "properties": {
                "currentHealthy": {"type": "integer"},
                "desiredHealthy": {"type": "integer"},
                "disruptionsAllowed": {"type": "integer"},
                "expectedPods": {"type": "integer"},
                "observedGeneration": {"type": "integer"},
                "disruptedPods": {"type": "object", "additionalProperties": {"type": "string"}},
                "conditions": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.policy.v1.PodDisruptionBudget": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"$ref": "#/definitions/io.k8s.api.policy.v1.PodDisruptionBudgetSpec"},
                "status": {"$ref": "#/definitions/io.k8s.api.policy.v1.PodDisruptionBudgetStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.ae.dev.v1alpha1.AppSpec": {
            "type": "object",
            "properties": {
                "image": {"type": "string"},
                "command": {"type": "array", "items": {"type": "string"}},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "array", "items": {"type": "object", "additionalProperties": {"type": "string"}}},
                "replicas": {"type": "integer"},
                "ports": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "health": {"type": "object", "additionalProperties": True},
                "service": {"type": "object", "additionalProperties": True},
                "ingress": {"type": "object", "additionalProperties": True},
                "resources": {"type": "object", "additionalProperties": True},
                "security": {"type": "object", "additionalProperties": True},
                "containers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "initContainers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "volumes": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "storage": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "emptyDirs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "networkPolicy": {"type": "object", "additionalProperties": True},
                "exportHints": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.ae.dev.v1alpha1.App": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"$ref": "#/definitions/io.k8s.api.ae.dev.v1alpha1.AppSpec"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.rbac.v1.Role": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "rules": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.rbac.v1.RoleBinding": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.rbac.v1.ClusterRole": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.rbac.v1.ClusterRoleBinding": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.networking.v1.IngressStatus": {
            "type": "object",
            "properties": {
                "loadBalancer": {"$ref": "#/definitions/io.k8s.api.core.v1.LoadBalancerStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.networking.v1.Ingress": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"type": "object", "additionalProperties": True},
                "status": {"$ref": "#/definitions/io.k8s.api.networking.v1.IngressStatus"},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.authorization.v1.SubjectAccessReview": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "spec": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": True,
        },
    }
    doc = {
        "swagger": "2.0",
        "info": {"title": "k1s apishim", "version": "0.1.0"},
        "produces": ["application/json"],
        "schemes": ["http"],
        "paths": {
            "/api/v1/namespaces": {"get": {}, "post": {}},
            "/api/v1/namespaces/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/api/v1/namespaces/{namespace}/configmaps": {"get": {}, "post": {}},
            "/api/v1/namespaces/{namespace}/configmaps/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/api/v1/namespaces/{namespace}/secrets": {"get": {}, "post": {}},
            "/api/v1/namespaces/{namespace}/secrets/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/api/v1/namespaces/{namespace}/serviceaccounts": {"get": {}, "post": {}},
            "/api/v1/namespaces/{namespace}/serviceaccounts/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/api/v1/namespaces/{namespace}/services": {"get": {}, "post": {}},
            "/api/v1/namespaces/{namespace}/services/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/apps/v1/namespaces/{namespace}/deployments": {"get": {}, "post": {}},
            "/apis/apps/v1/namespaces/{namespace}/deployments/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/apps/v1/namespaces/{namespace}/statefulsets": {"get": {}, "post": {}},
            "/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/apps/v1/namespaces/{namespace}/daemonsets": {"get": {}, "post": {}},
            "/apis/apps/v1/namespaces/{namespace}/daemonsets/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/batch/v1/namespaces/{namespace}/jobs": {"get": {}, "post": {}},
            "/apis/batch/v1/namespaces/{namespace}/jobs/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/batch/v1/namespaces/{namespace}/cronjobs": {"get": {}, "post": {}},
            "/apis/batch/v1/namespaces/{namespace}/cronjobs/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers": {"get": {}, "post": {}},
            "/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets": {"get": {}, "post": {}},
            "/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/rbac.authorization.k8s.io/v1/clusterroles": {"get": {}, "post": {}},
            "/apis/rbac.authorization.k8s.io/v1/clusterroles/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings": {"get": {}, "post": {}},
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles": {"get": {}, "post": {}},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings": {"get": {}, "post": {}},
            "/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses": {"get": {}, "post": {}},
            "/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
            "/apis/authorization.k8s.io/v1/subjectaccessreviews": {"post": {}},
            "/apis/ae.dev/v1alpha1/namespaces/{namespace}/apps": {"get": {}, "post": {}},
            "/apis/ae.dev/v1alpha1/namespaces/{namespace}/apps/{name}": {"get": {}, "delete": {}, "patch": {}, "put": {}},
        },
        "definitions": schemas,
    }
    return doc


def _openapi_v3_stub() -> dict[str, Any]:
    doc = _swagger_doc()
    return {
        "openapi": "3.0.0",
        "info": doc.get("info", {}),
        "paths": doc.get("paths", {}),
        "components": {"schemas": doc.get("definitions", {})},
        "x-k1s-note": "OpenAPI v3 mirrors /openapi/v2 and is kept authoritative alongside it",
    }


@dataclass
class Principal:
    username: str
    groups: set[str]
    token_role: str | None
    token: str | None


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "k1s-apishim"
    admin_token: str | None = os.getenv("AE_APISHIM_TOKEN")
    read_token: str | None = os.getenv("AE_APISHIM_READ_TOKEN")
    allow_anonymous: bool = os.getenv("AE_APISHIM_ALLOW_ANON", "0") == "1"
    rbac_enabled: bool = os.getenv("AE_APISHIM_RBAC", "0") == "1"
    rbac_eval_roles: bool = os.getenv("AE_APISHIM_RBAC_EVAL", "0") == "1"
    sa_tokens: dict[str, tuple[str, str, float]] = {}
    sa_tokens_lock = threading.RLock()
    sa_token_ttl: int = int(os.getenv("AE_APISHIM_SA_TOKEN_TTL", "3600") or "3600")
    # Simple in-memory RBAC rules: (verb, resource) -> allowed roles
    rbac_policies: dict[tuple[str, str], set[str]] = {
        ("get", "*"): {"admin", "read"},
        ("list", "*"): {"admin", "read"},
        ("watch", "*"): {"admin", "read"},
        ("create", "*"): {"admin"},
        ("create", "pods/exec"): {"admin"},
        ("create", "pods/portforward"): {"admin"},
        ("create", "services/portforward"): {"admin"},
        ("update", "*"): {"admin"},
        ("patch", "*"): {"admin"},
        ("delete", "*"): {"admin"},
    }
    store: ObjectStore
    state: SQLiteStateStore
    client_cert_required: bool = False
    crd_registry: dict[tuple[str, str, str], dict[str, Any]] = {}
    crd_index: dict[str, list[tuple[str, str, str]]] = {}
    crd_lock = threading.RLock()

    def _parse_principal(self) -> Principal:
        hdr = self.headers.get("Authorization", "")
        tok = hdr[7:] if hdr.startswith("Bearer ") else ""
        username = "system:unauthenticated"
        groups: set[str] = {"system:unauthenticated"}
        token_role: str | None = None
        if tok and tok == self.admin_token:
            username = "admin"
            groups = {"system:authenticated", "admin"}
            token_role = "admin"  # noqa: S105 - role label, not a secret
        elif tok and tok == self.read_token:
            username = "reader"
            groups = {"system:authenticated", "read"}
            token_role = "read"  # noqa: S105 - role label, not a secret
        else:
            with self.sa_tokens_lock:
                sa = self.sa_tokens.get(tok)
            if sa:
                ns, name, exp_ts = sa
                if exp_ts < time.time():
                    # expired; drop it
                    with self.sa_tokens_lock:
                        self.sa_tokens.pop(tok, None)
                    return Principal(username="system:unauthenticated", groups={"system:unauthenticated"}, token_role=None, token=None)
                username = f"system:serviceaccount:{ns}:{name}"
                groups = {
                    "system:authenticated",
                    "system:serviceaccounts",
                    f"system:serviceaccounts:{ns}",
                }
                token_role = None
        return Principal(username=username, groups=groups, token_role=token_role, token=tok)

    def _authz(self, role: str = "read") -> bool:
        admin = self.admin_token
        reader = self.read_token
        # When anonymous access is enabled, allow requests to proceed even if tokens
        # are configured. This keeps dev/test flows (like the helm shim smoke test)
        # working without having to inject auth headers everywhere.
        if self.allow_anonymous and not admin and not reader:
            return True
        principal = self._parse_principal()
        role_name = principal.token_role
        ok = False
        if role == "write":
            ok = role_name == "admin"
        elif role == "read":
            ok = role_name in {"admin", "read"}
        elif role in {"rbac-read", "rbac-write"}:
            ok = role_name in {"admin", "read"}
        if ok:
            return True
        if self.allow_anonymous:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", "Bearer")
        self._json_status(
            HTTPStatus.UNAUTHORIZED,
            reason="Unauthorized",
            message="missing/invalid bearer token",
        )
        return False

    def _eval_subject_access_review(self, spec: dict[str, Any]) -> dict[str, Any]:
        res_attr = (spec or {}).get("resourceAttributes") or {}
        verb = (res_attr.get("verb") or "").lower()
        resource = res_attr.get("resource") or ""
        subres = res_attr.get("subresource")
        namespace = res_attr.get("namespace")
        if subres:
            resource = f"{resource}/{subres}"
        if not verb or not resource:
            return {"allowed": False, "denied": True, "reason": "missing verb/resource"}
        allowed = self._rbac_allows(verb, resource, namespace)
        return {"allowed": allowed, "denied": not allowed, "reason": "rbac: allowed" if allowed else "rbac: forbidden"}

    def _issue_sa_token(self, namespace: str, name: str) -> str:
        token = secrets.token_urlsafe(32)
        exp_ts = time.time() + self.sa_token_ttl
        with self.sa_tokens_lock:
            self.sa_tokens[token] = (namespace, name, exp_ts)
        return token

    def _rbac_allows(self, verb: str, resource: str, namespace: str | None = None) -> bool:
        if not self.rbac_enabled:
            return True
        principal = self._parse_principal()
        role = principal.token_role
        if role is None:
            return False
        # Static policy fallback
        if not self.rbac_eval_roles:
            allowed = self.rbac_policies.get((verb, resource)) or self.rbac_policies.get((verb, "*"))
            return bool(allowed and role in allowed)
        # Role/RoleBinding evaluation
        user = principal.username
        groups = principal.groups
        # Collect role rules from bindings
        allowed_verbs: set[str] = set()
        try:
            # RoleBindings (namespaced)
            for rb in self.store.list_all("rbac.authorization.k8s.io", "v1", "rolebindings"):  # type: ignore[attr-defined]
                if namespace and rb.namespace and rb.namespace != namespace:
                    continue
                subjects = (rb.spec or {}).get("subjects", [])
                if not any(
                    (s.get("kind") == "User" and s.get("name") == user)
                    or (s.get("kind") == "Group" and s.get("name") in groups)
                for s in subjects):
                    continue
                ref = (rb.spec or {}).get("roleRef", {})
                rname = ref.get("name")
                if not rname:
                    continue
                role_obj = self.store.get("rbac.authorization.k8s.io", "v1", "roles", rb.namespace, rname)  # type: ignore[attr-defined]
                if role_obj:
                    for rule in (role_obj.spec or {}).get("rules", []):
                        if _rule_matches(resource, rule.get("resources", [])):
                            allowed_verbs.update(rule.get("verbs", []))
            # ClusterRoleBindings
            for crb in self.store.list_all("rbac.authorization.k8s.io", "v1", "clusterrolebindings"):  # type: ignore[attr-defined]
                subjects = (crb.spec or {}).get("subjects", [])
                if not any(
                    (s.get("kind") == "User" and s.get("name") == user)
                    or (s.get("kind") == "Group" and s.get("name") in groups)
                for s in subjects):
                    continue
                ref = (crb.spec or {}).get("roleRef", {})
                rname = ref.get("name")
                if not rname:
                    continue
                crobj = self.store.get("rbac.authorization.k8s.io", "v1", "clusterroles", None, rname)  # type: ignore[attr-defined]
                if crobj:
                    for rule in (crobj.spec or {}).get("rules", []):
                        if _rule_matches(resource, rule.get("resources", [])):
                            allowed_verbs.update(rule.get("verbs", []))
        except Exception:
            return False
        if not allowed_verbs:
            # fallback to static if no rules matched
            allowed = self.rbac_policies.get((verb, resource)) or self.rbac_policies.get((verb, "*"))
            return bool(allowed and role in allowed)
        return verb in allowed_verbs

    def _ok(self, payload: dict[str, Any]) -> None:
        data = _json(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, msg: str = "not found") -> None:
        self._json_status(HTTPStatus.NOT_FOUND, reason="NotFound", message=msg)

    def _json_status(self, code: int, *, reason: str, message: str) -> None:
        body = {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure" if 400 <= code else "Success",
            "message": message,
            "reason": reason,
            "code": code,
        }
        data = _json(body)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        """Read request body handling both Content-Length and chunked transfer."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            body = bytearray()
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    chunk_len = int(line.strip(), 16)
                except Exception:
                    break
                if chunk_len == 0:
                    # consume trailing CRLF after last chunk
                    self.rfile.readline()
                    break
                body.extend(self.rfile.read(chunk_len))
                # consume chunk trailer CRLF
                self.rfile.read(2)
            return bytes(body)
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length)

    def _deny(self, code: int = HTTPStatus.FORBIDDEN, message: str = "forbidden") -> None:
        self._json_status(int(code), reason="Forbidden" if int(code) == 403 else "Unauthorized", message=message)

    def _max_rv_for(self, group: str, version: str, resource: str, namespace: str | None) -> int:
        try:
            if namespace is None:
                items = self.server.store.list_all(group, version, resource)  # type: ignore[attr-defined]
            else:
                items = self.server.store.list(group, version, resource, namespace)  # type: ignore[attr-defined]
            return max((i.resource_version for i in items), default=0)
        except Exception:
            return 0

    # ---------------- WebSocket port-forward (best-effort) ----------------
    def _handle_port_forward_ws(self, target_host: str, target_port: int) -> None:
        """Minimal WebSocket port-forward bridge (single connection, multi-port)."""

        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return
        try:
            with open("/tmp/pf-headers.log", "w") as hdr:
                for k, v in self.headers.items():
                    hdr.write(f"{k}: {v}\n")
        except Exception:
            pass
        accept_seed = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("utf-8")
        accept = base64.b64encode(hashlib.sha1(accept_seed).digest()).decode("utf-8")  # noqa: S324 - RFC 6455 requires SHA-1
        subproto_hdr = self.headers.get("Sec-WebSocket-Protocol")
        chosen_proto = None
        if subproto_hdr:
            chosen_proto = subproto_hdr.split(",")[0].strip()

        def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        def _recv_ws(sock: socket.socket) -> tuple[int, bytes] | None:
            try:
                hdr = _recv_exact(sock, 2)
                if not hdr:
                    return None
                opcode = hdr[0] & 0x0F
                masked = bool(hdr[1] & 0x80)
                length = hdr[1] & 0x7F
                if length == 126:
                    ext = _recv_exact(sock, 2)
                    if ext is None:
                        return None
                    length = int.from_bytes(ext, "big")
                elif length == 127:
                    ext = _recv_exact(sock, 8)
                    if ext is None:
                        return None
                    length = int.from_bytes(ext, "big")
                mask = _recv_exact(sock, 4) if masked else b""
                payload = _recv_exact(sock, length) if length else b""
                if payload is None:
                    return None
                if masked and mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                return opcode, payload
            except Exception:
                return None

        def _send_ws(sock: socket.socket, payload: bytes, opcode: int = 0x2) -> None:
            try:
                header = bytearray()
                header.append(0x80 | (opcode & 0x0F))
                l = len(payload)
                if l < 126:
                    header.append(l)
                elif l < (1 << 16):
                    header.append(126)
                    header.extend(l.to_bytes(2, "big"))
                else:
                    header.append(127)
                    header.extend(l.to_bytes(8, "big"))
                sock.sendall(header + payload)
            except Exception:
                pass

        # Handshake
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if chosen_proto:
            self.send_header("Sec-WebSocket-Protocol", chosen_proto)
        self.end_headers()

        # SPDY-over-WebSocket tunneling (SPDY/3.1+portforward.k8s.io)
        if chosen_proto and chosen_proto.startswith("SPDY/3.1+"):
            class WsConn:
                def __init__(self, sock, recv_fn, send_fn):
                    self._sock = sock
                    self._recv_fn = recv_fn
                    self._send_fn = send_fn
                    self._buf = b""

                def settimeout(self, t: float) -> None:  # pragma: no cover - best-effort
                    try:
                        self._sock.settimeout(t)
                    except Exception:
                        pass

                def recv(self, n: int) -> bytes:
                    while len(self._buf) < n:
                        msg = self._recv_fn(self._sock)
                        if msg is None:
                            return b""
                        opcode, payload = msg
                        if opcode == 0x8:
                            return b""
                        if opcode not in (0x1, 0x2) or not payload:
                            continue
                        self._buf += payload
                    out, self._buf = self._buf[:n], self._buf[n:]
                    return out

                def sendall(self, data: bytes) -> None:
                    self._send_fn(self._sock, data, opcode=0x2)

            ws_conn = WsConn(self.connection, _recv_ws, _send_ws)
            try:
                self._handle_port_forward_spdy(target_host, [target_port], conn_override=ws_conn, suppress_handshake=True)
            finally:
                try:
                    self.connection.close()
                except Exception:
                    pass
            return

        # Per-port upstream sockets
        upstream_socks: dict[int, socket.socket] = {}
        stop = False

        def _get_upstream(port: int) -> socket.socket | None:
            if port in upstream_socks:
                return upstream_socks[port]
            try:
                s = socket.create_connection((target_host, port), timeout=5.0)
                s.settimeout(0.1)
                upstream_socks[port] = s
                return s
            except Exception:
                return None

        def _pump_from_client() -> None:
            nonlocal stop
            while not stop:
                msg = _recv_ws(self.connection)
                if msg is None:
                    break
                opcode, payload = msg
                if opcode == 0x8:  # close
                    stop = True
                    break
                if opcode not in (0x1, 0x2) or len(payload) < 2:
                    continue
                try:
                    with open("/tmp/pf-debug.log", "ab") as dbg:
                        dbg.write(payload + b"\n")
                except Exception:
                    pass
                port = int.from_bytes(payload[:2], "big")
                data = payload[2:]
                sock = _get_upstream(port or target_port)
                if sock and data:
                    try:
                        sock.sendall(data)
                    except Exception:
                        stop = True
                        break

        def _pump_to_client(port: int, sock: socket.socket) -> None:
            nonlocal stop
            while not stop:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    frame = port.to_bytes(2, "big") + chunk
                    _send_ws(self.connection, frame, opcode=0x2)
                except socket.timeout:
                    continue
                except Exception:
                    break

        # Start client->upstream pump
        t_client = threading.Thread(target=_pump_from_client, daemon=True)
        t_client.start()

        # Keep reading from upstream sockets that exist; create one for initial target_port
        first_sock = _get_upstream(target_port)
        threads: list[threading.Thread] = []
        if first_sock:
            t = threading.Thread(target=_pump_to_client, args=(target_port, first_sock), daemon=True)
            t.start()
            threads.append(t)
        # Join while client pump alive
        try:
            t_client.join(timeout=5)
        except Exception:
            pass
        stop = True
        for s in upstream_socks.values():
            try:
                s.close()
            except Exception:
                pass
        try:
            self.connection.close()
        except Exception:
            pass

    # ---------------- SPDY/3.1 port-forward (kubectl) ----------------
    def _handle_port_forward_spdy(self, target_host: str, target_ports: list[int], target_hosts_by_port: dict[int, str] | None = None, *, conn_override=None, suppress_handshake: bool = False) -> None:
        """Implements the SPDY/3.1 port-forward protocol used by kubectl.

        Each data stream carries raw TCP bytes to a target port advertised in
        SYN_STREAM headers ("port" or "streamname"). Error streams mirror the
        port-specific error channel. We allow ports from `target_ports` only to
        avoid surprising exposure when kubectl requests multiple ports.
        """

        conn = conn_override or self.connection
        if not suppress_handshake:
            # Accept upgrade after basic validation
            self.send_response(101, "Switching Protocols")
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", self.headers.get("Upgrade", "SPDY/3.1"))
            self.end_headers()
        try:
            conn.settimeout(0.05)
        except Exception:
            pass

        if not target_ports:
            target_ports = [0]

        SPDY_DICT = (
            b"optionsgetheadpostputdeletetraceacceptaccept-charsetaccept-encodingaccept-language"
            b"authorizationexpectfromhostif-modified-sinceif-matchif-none-matchif-rangeif-unmodified-"
            b"sincemax-forwardsproxy-authorizationrange refererteuser-agent100101200201202203204205206"
            b"300301302303304305306307400401402403404405406407408409410411412413414415416417500501502"
            b"503504505accept-rangesageetaglocationproxy-authenticatepublicretry-afterservervarywarning"
            b"www-authenticateallowcontent-basecontent-encodingcache-controlconnectiondatetrailertransfer"
            b"-encodingupgradeviawarningcontent-languagecontent-lengthcontent-locationcontent-md5content-"
            b"rangecontent-typeetagexpireslast-modifiedset-cookieMondayTuesdayWednesdayThursdayFridaySaturday"
            b"SundayJanFebMarAprMayJunJulAugSepOctNovDecchunkedtext/htmlimage/pngimage/jpgimage/gifapplication"
            b"/xmlapplication/xhtmltext/plainpublicprivatemax-agegztcomparallel bytesruning"
        )
        dctx = zlib.decompressobj(zdict=SPDY_DICT)

        window_size = 1 << 20  # 1MiB default
        stream_windows: dict[int, int] = {}
        data_streams: dict[int, int] = {}  # stream_id -> target_port
        error_streams: dict[int, int] = {}  # stream_id -> data_stream sid
        upstream_cache: dict[int, socket.socket] = {}
        host_by_port = target_hosts_by_port or {}
        # Build round-robin host cycles per port when multiple endpoints exist
        host_cycle: dict[int, list[str]] = {}
        for p, h in host_by_port.items():
            if isinstance(h, list):
                host_cycle[p] = list(h)
            else:
                host_cycle[p] = [h]

        def read_exact(sock, n: int) -> bytes | None:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        def send_data_frame(stream_id: int, payload: bytes, flags: int = 0) -> None:
            header = bytearray()
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header.append(flags & 0xFF)
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        def send_window_update(stream_id: int, delta: int) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x09).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (delta & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))
            stream_windows[stream_id] = stream_windows.get(stream_id, window_size) + delta

        def send_ping(opaque: bytes = b"\x00\x00\x00\x01") -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x06).to_bytes(2, "big")
            header += b"\x00"
            header += (4).to_bytes(3, "big")
            header += opaque[:4]
            conn.sendall(bytes(header))

        def send_settings(settings: dict[int, int]) -> None:
            payload = bytearray()
            payload += len(settings).to_bytes(4, "big")
            for sid, val in settings.items():
                payload.append(0)
                payload += sid.to_bytes(2, "big")
                payload += val.to_bytes(4, "big")
            header = bytearray()
            header += b"\x80\x03"
            header += (0x04).to_bytes(2, "big")
            header += b"\x00"
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        def send_rst(stream_id: int, code: int = 2) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x03).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (code & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def send_goaway(last_stream: int = 0, status: int = 0) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x07).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (last_stream & 0x7FFFFFFF).to_bytes(4, "big")
            header += (status & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def parse_syn_stream(payload: bytes) -> dict[str, str]:
            headers: dict[str, str] = {}
            if len(payload) < 10:
                return headers
            header_block = payload[10:]
            try:
                decompressed = dctx.decompress(header_block)
                import io

                f = io.BytesIO(decompressed)
                num = int.from_bytes(f.read(4), "big")
                for _ in range(num):
                    nlen = int.from_bytes(f.read(4), "big")
                    name = f.read(nlen).decode("utf-8", "ignore")
                    vlen = int.from_bytes(f.read(4), "big")
                    value = f.read(vlen).decode("utf-8", "ignore")
                    headers[name] = value
            except Exception:
                return headers
            return headers

        try:
            send_settings({0x04: window_size})  # advertise window
            last_ping = time.time()
            while True:
                now = time.time()
                if now - last_ping > 10:
                    try:
                        send_ping()
                    except Exception:
                        break
                    last_ping = now

                try:
                    hdr = conn.recv(8)
                except TimeoutError:
                    hdr = None
                if hdr:
                    if len(hdr) < 8:
                        break
                    is_control = (hdr[0] & 0x80) != 0
                    if is_control:
                        frame_type = int.from_bytes(hdr[2:4], "big")
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        if length > (1 << 20):
                            send_goaway(status=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if frame_type == 1:  # SYN_STREAM
                            sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                            headers = parse_syn_stream(payload)
                            stype = headers.get("streamtype", "").lower()
                            try:
                                port = int(headers.get("port") or headers.get("streamname") or target_ports[0])
                            except Exception:
                                port = target_ports[0]
                            if port not in target_ports and target_ports[0] != 0:
                                send_rst(sid, code=2)
                                continue
                            choices = host_cycle.get(port) or [host_by_port.get(port, target_host)]
                            # simple round-robin by rotating list
                            if len(choices) > 1:
                                choices.append(choices.pop(0))
                                host_cycle[port] = choices
                            if stype == "data":
                                data_streams[sid] = port
                                stream_windows[sid] = window_size
                            elif stype == "error":
                                error_streams[sid] = data_streams.get(sid - 1, port)
                        elif frame_type == 4:  # SETTINGS
                            try:
                                num = int.from_bytes(payload[0:4], "big")
                                idx = 4
                                for _ in range(num):
                                    if idx + 8 > len(payload):
                                        break
                                    _flags = payload[idx]
                                    sid_setting = int.from_bytes(payload[idx + 1:idx + 3], "big")
                                    val = int.from_bytes(payload[idx + 3:idx + 7], "big")
                                    idx += 8
                                    if sid_setting == 0x04:
                                        window_size = val
                            except Exception:
                                pass
                        elif frame_type == 9:  # WINDOW_UPDATE
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                delta = int.from_bytes(payload[4:8], "big")
                                stream_windows[sid] = stream_windows.get(sid, window_size) + delta
                                if stream_windows[sid] > (1 << 24):
                                    send_rst(sid, code=2)
                        elif frame_type == 3:  # RST_STREAM
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                upstream_sock = upstream_cache.pop(sid, None)
                                if upstream_sock:
                                    try:
                                        upstream_sock.close()
                                    except Exception:
                                        pass
                                data_streams.pop(sid, None)
                                error_streams.pop(sid, None)
                        elif frame_type == 6:  # PING
                            send_ping(payload[:4])
                        elif frame_type == 7:  # GOAWAY
                            break
                        if flags & 0x01:  # FIN on control frame
                            continue
                    else:
                        stream_id = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        if length > (1 << 20):
                            send_rst(stream_id, code=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if stream_id in data_streams and payload:
                            port = data_streams[stream_id]
                            wnd = stream_windows.get(stream_id, window_size)
                            if wnd <= 0:
                                send_rst(stream_id, code=2)
                                continue
                            if stream_id not in upstream_cache:
                                try:
                                    upstream_cache[stream_id] = socket.create_connection((target_host, port), timeout=5.0)
                                    upstream_cache[stream_id].settimeout(0.05)
                                except Exception:
                                    upstream_cache.pop(stream_id, None)
                                    continue
                            try:
                                upstream_cache[stream_id].sendall(payload)
                                stream_windows[stream_id] = max(0, wnd - len(payload))
                            except Exception:
                                send_rst(stream_id, code=2)
                                continue
                            try:
                                send_window_update(stream_id, len(payload))
                            except Exception:
                                pass
                        if flags & 0x02:  # FIN
                            upstream_sock = upstream_cache.pop(stream_id, None)
                            if upstream_sock:
                                try:
                                    upstream_sock.close()
                                except Exception:
                                    pass
                            send_rst(stream_id, code=0)

                # Pull from upstream sockets and forward to client
                for sid, sock_up in list(upstream_cache.items()):
                    try:
                        resp = sock_up.recv(4096)
                        if resp:
                            send_data_frame(sid, resp, flags=0)
                            try:
                                send_window_update(sid, len(resp))
                            except Exception:
                                pass
                        else:
                            upstream_cache.pop(sid, None)
                            try:
                                sock_up.close()
                            except Exception:
                                pass
                            for esid, dport in list(error_streams.items()):
                                if data_streams.get(sid) == dport:
                                    try:
                                        send_data_frame(esid, b"", flags=0x01)
                                    except Exception:
                                        pass
                    except TimeoutError:
                        continue
                    except Exception:
                        upstream_cache.pop(sid, None)
                        try:
                            sock_up.close()
                        except Exception:
                            pass
        finally:
            try:
                send_goaway(last_stream=max(data_streams.keys()) if data_streams else 0, status=0)
            except Exception:
                pass
            for s in upstream_cache.values():
                try:
                    s.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    # ---------------- SPDY/3.1 exec/attach (kubectl exec) ----------------
    def _handle_exec_spdy(
        self,
        *,
        pod_name: str,
        command: list[str],
        container: str | None,
        tty: bool,
        want_stdin: bool,
        want_stdout: bool,
        want_stderr: bool,
    ) -> None:
        # Try to open an attached exec session on the runtime (docker/podman only for now)
        exec_sock = None
        exec_id = None
        if hasattr(self.server.runtime, "exec_attach"):  # type: ignore[attr-defined]
            try:
                exec_sock, exec_id = self.server.runtime.exec_attach(  # type: ignore[attr-defined]
                    pod_name, command, container=container, tty=tty
                )
                exec_sock.settimeout(0.05)
            except Exception:
                exec_sock = None
        if exec_sock is None:
            self._json_status(
                HTTPStatus.NOT_IMPLEMENTED,
                reason="NotImplemented",
                message="Streaming exec not available for this runtime",
            )
            return

        # Accept upgrade after we know we can serve it
        self.send_response(101, "Switching Protocols")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", self.headers.get("Upgrade", "SPDY/3.1"))
        self.end_headers()

        conn = self.connection
        conn.settimeout(0.05)

        SPDY_DICT = (
            b"optionsgetheadpostputdeletetraceacceptaccept-charsetaccept-encodingaccept-language"
            b"authorizationexpectfromhostif-modified-sinceif-matchif-none-matchif-rangeif-unmodified-"
            b"sincemax-forwardsproxy-authorizationrange refererteuser-agent100101200201202203204205206"
            b"300301302303304305306307400401402403404405406407408409410411412413414415416417500501502"
            b"503504505accept-rangesageetaglocationproxy-authenticatepublicretry-afterservervarywarning"
            b"www-authenticateallowcontent-basecontent-encodingcache-controlconnectiondatetrailertransfer"
            b"-encodingupgradeviawarningcontent-languagecontent-lengthcontent-locationcontent-md5content-"
            b"rangecontent-typeetagexpireslast-modifiedset-cookieMondayTuesdayWednesdayThursdayFridaySaturday"
            b"SundayJanFebMarAprMayJunJulAugSepOctNovDecchunkedtext/htmlimage/pngimage/jpgimage/gifapplication"
            b"/xmlapplication/xhtmltext/plainpublicprivatemax-agegztcomparallel bytesruning"
        )
        dctx = zlib.decompressobj(zdict=SPDY_DICT)

        stream_ids: dict[str, int] = {}  # streamtype -> sid
        window_size = 1 << 20
        stream_windows: dict[int, int] = {}
        resize_sid: int | None = None

        def read_exact(sock, n: int) -> bytes | None:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            return buf

        def send_data_frame(stream_id: int, payload: bytes, flags: int = 0) -> None:
            header = bytearray()
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header.append(flags & 0xFF)
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        def send_window_update(stream_id: int, delta: int) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x09).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (delta & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))
            stream_windows[stream_id] = stream_windows.get(stream_id, window_size) + delta

        def send_ping(opaque: bytes = b"\x00\x00\x00\x01") -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x06).to_bytes(2, "big")
            header += b"\x00"
            header += (4).to_bytes(3, "big")
            header += opaque[:4]
            conn.sendall(bytes(header))

        def send_rst(stream_id: int, code: int = 2) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x03).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            header += (code & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def send_goaway(last_stream: int = 0, status: int = 0) -> None:
            header = bytearray()
            header += b"\x80\x03"
            header += (0x07).to_bytes(2, "big")
            header += b"\x00"
            header += (8).to_bytes(3, "big")
            header += (last_stream & 0x7FFFFFFF).to_bytes(4, "big")
            header += (status & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header))

        def parse_syn_stream(payload: bytes) -> dict[str, str]:
            headers: dict[str, str] = {}
            if len(payload) < 10:
                return headers
            header_block = payload[10:]
            try:
                decompressed = dctx.decompress(header_block)
                import io

                f = io.BytesIO(decompressed)
                num = int.from_bytes(f.read(4), "big")
                for _ in range(num):
                    nlen = int.from_bytes(f.read(4), "big")
                    name = f.read(nlen).decode("utf-8", "ignore")
                    vlen = int.from_bytes(f.read(4), "big")
                    value = f.read(vlen).decode("utf-8", "ignore")
                    headers[name] = value
            except Exception:
                return headers
            return headers

        def demux_exec_frame(frame: bytes) -> tuple[int, bytes] | None:
            # Docker multiplexed attach header: 1 byte stream, 3 bytes zero, 4 bytes length
            if len(frame) < 8:
                return None
            stream_type = frame[0]
            size = int.from_bytes(frame[4:8], "big")
            if size == 0:
                return None
            data = frame[8:8 + size]
            if len(data) < size:
                return None
            return stream_type, data

        last_ping = time.time()
        try:
            exec_buf = b""
            while True:
                now = time.time()
                if now - last_ping > 10:
                    try:
                        send_ping()
                    except Exception:
                        break
                    last_ping = now

                # Read SPDY control/data frames from client
                try:
                    hdr = conn.recv(8)
                except TimeoutError:
                    hdr = None
                if hdr:
                    if len(hdr) < 8:
                        break
                    is_control = (hdr[0] & 0x80) != 0
                    if is_control:
                        frame_type = int.from_bytes(hdr[2:4], "big")
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        if length > (1 << 20):
                            send_goaway(status=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if frame_type == 1:  # SYN_STREAM registers channels
                            sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                            headers = parse_syn_stream(payload)
                            stype = headers.get("streamtype", "").lower()
                            stream_ids[stype] = sid
                            stream_windows[sid] = window_size
                            if stype == "resize":
                                resize_sid = sid
                        elif frame_type == 4:  # SETTINGS
                            try:
                                num = int.from_bytes(payload[0:4], "big")
                                idx = 4
                                for _ in range(num):
                                    if idx + 8 > len(payload):
                                        break
                                    _flags = payload[idx]
                                    sid_setting = int.from_bytes(payload[idx + 1:idx + 3], "big")
                                    val = int.from_bytes(payload[idx + 3:idx + 7], "big")
                                    idx += 8
                                    if sid_setting == 0x04:
                                        window_size = val
                            except Exception:
                                pass
                        elif frame_type == 9:  # WINDOW_UPDATE
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                delta = int.from_bytes(payload[4:8], "big")
                                stream_windows[sid] = stream_windows.get(sid, window_size) + delta
                        elif frame_type == 3:  # RST_STREAM
                            if len(payload) >= 8:
                                sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                                if sid == stream_ids.get("stdin"):
                                    try:
                                        exec_sock.shutdown(socket.SHUT_WR)
                                    except Exception:
                                        pass
                        elif frame_type == 6:  # PING
                            conn.sendall(hdr + payload)  # echo
                        elif frame_type == 7:  # GOAWAY
                            break
                    else:
                        stream_id = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        payload = read_exact(conn, length) or b""
                        # STDIN frames
                        if stream_id == stream_ids.get("stdin") and want_stdin and payload:
                            try:
                                exec_sock.sendall(payload)
                            except Exception:
                                pass
                        elif resize_sid is not None and stream_id == resize_sid:
                            # Handle resize payload JSON {"Width":x,"Height":y}
                            try:
                                doc = json.loads(payload.decode("utf-8", "ignore"))
                                h = int(doc.get("Height")) if doc.get("Height") is not None else None
                                w = int(doc.get("Width")) if doc.get("Width") is not None else None
                                if hasattr(self.server.runtime, "exec_resize"):  # type: ignore[attr-defined]
                                    try:
                                        self.server.runtime.exec_resize(exec_id or "", height=h, width=w)  # type: ignore[attr-defined]
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        if flags & 0x02:  # FIN from client
                            if stream_id == stream_ids.get("stdin"):
                                try:
                                    exec_sock.shutdown(socket.SHUT_WR)
                                except Exception:
                                    pass

                # Read from exec socket and forward to stdout/stderr
                try:
                    chunk = exec_sock.recv(4096)
                except TimeoutError:
                    chunk = None
                except Exception:
                    chunk = b""
                if chunk:
                    exec_buf += chunk
                    if tty:
                        if want_stdout:
                            sid = stream_ids.get("stdout")
                            if sid:
                                send_data_frame(sid, chunk, flags=0)
                                try:
                                    send_window_update(sid, len(chunk))
                                except Exception:
                                    pass
                    else:
                        while True:
                            if len(exec_buf) < 8:
                                break
                            size = int.from_bytes(exec_buf[4:8], "big")
                            frame_len = 8 + size
                            if len(exec_buf) < frame_len:
                                break
                            frame = exec_buf[:frame_len]
                            exec_buf = exec_buf[frame_len:]
                            dm = demux_exec_frame(frame)
                            if dm:
                                stype, data = dm
                                if stype == 1 and want_stdout:
                                    sid = stream_ids.get("stdout")
                                elif stype == 2 and want_stderr:
                                    sid = stream_ids.get("stderr")
                                else:
                                    sid = None
                                if sid and data:
                                    send_data_frame(sid, data, flags=0)
                                    try:
                                        send_window_update(sid, len(data))
                                    except Exception:
                                        pass
                elif chunk == b"":
                    break

        finally:
            # Send exit status over error stream if present
            exit_code = 0
            try:
                if exec_id and hasattr(self.server.runtime, "exec_exit_code"):  # type: ignore[attr-defined]
                    exit_code = int(self.server.runtime.exec_exit_code(exec_id))  # type: ignore[attr-defined]
            except Exception:
                exit_code = 0
            err_sid = stream_ids.get("error")
            if err_sid:
                status_obj = {
                    "metadata": {},
                    "status": "Success",
                    "message": "",
                    "reason": "",
                    "code": exit_code,
                    "details": {"exitCode": exit_code},
                }
                try:
                    send_data_frame(err_sid, json.dumps(status_obj, separators=(",", ":")).encode("utf-8"), flags=0x02)
                except Exception:
                    pass
            try:
                send_goaway(last_stream=max(stream_ids.values()) if stream_ids else 0, status=0)
            except Exception:
                pass
            if exec_sock:
                try:
                    exec_sock.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    def _stream_watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        query: dict[str, list[str]],
        transform,
    ) -> None:
        # Watches use latest observed rv; no pagination
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            start = time.time()
            timeout = int(query.get("timeoutSeconds", ["0"])[0] or 0) or None
            heartbeat = int(query.get("heartbeatSeconds", ["0"])[0] or 0) or None
            allow_bm = query.get("allowWatchBookmarks", ["0"])[0] in ("1", "true", "True")
            rv_param = query.get("resourceVersion", [""])[0] or None
            # Emit initial bookmark if requested/allowed
            if allow_bm:
                try:
                    current = self._max_rv_for(group, version, resource, namespace)
                except Exception:
                    current = 0
                initial_rv = rv_param if rv_param else str(current)
                bm = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": str(initial_rv)}}}
                self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()
            for ev_type, obj in self.server.store.watch(group, version, resource, namespace, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm, since_rv=int(rv_param) if rv_param and rv_param.isdigit() else None):  # type: ignore[attr-defined]
                body = {"type": ev_type, "object": transform(obj)}
                line = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
                self.wfile.write(line)
                self.wfile.flush()
                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                    break
        except BrokenPipeError:
            pass

    def _stream_fake_watch(self, objs: list[dict[str, Any]], kind: str, api_version: str) -> None:
        """Minimal watch emulation for derived resources like EndpointSlice."""
        try:
            for obj in objs:
                ev = {"type": "ADDED", "object": obj}
                self.wfile.write(json.dumps(ev, separators=(",", ":")).encode("utf-8") + b"\n")
            # bookmark at end
            rv = max((int(o.get("metadata", {}).get("resourceVersion", "0")) for o in objs), default=0)
            bm = {"type": "BOOKMARK", "object": {"kind": kind, "apiVersion": api_version, "metadata": {"resourceVersion": str(rv)}}}
            self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def _serve_dynamic_group_discovery(self, path: str) -> bool:
        m_group = re.match(r"^/apis/([^/]+)$", path)
        if m_group:
            group = m_group.group(1)
            versions = self._crd_versions_for_group(group)
            if not versions:
                if group == "discovery.k8s.io":
                    self._ok(
                        {
                            "kind": "APIGroup",
                            "apiVersion": "v1",
                            "name": "discovery.k8s.io",
                            "versions": [{"groupVersion": "discovery.k8s.io/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "discovery.k8s.io/v1", "version": "v1"},
                            "serverAddressByClientCIDRs": [],
                        }
                    )
                    return True
                return False
            payload = {
                "kind": "APIGroup",
                "apiVersion": "v1",
                "name": group,
                "versions": [{"groupVersion": f"{group}/{ver}", "version": ver} for ver in versions],
                "preferredVersion": {"groupVersion": f"{group}/{versions[0]}", "version": versions[0]},
                "serverAddressByClientCIDRs": [],
            }
            self._ok(payload)
            return True
        m_version = re.match(r"^/apis/([^/]+)/([^/]+)$", path)
        if m_version:
            group, version = m_version.group(1), m_version.group(2)
            if group == "discovery.k8s.io" and version == "v1":
                self._ok(
                    {
                        "kind": "APIResourceList",
                        "apiVersion": "discovery.k8s.io/v1",
                        "groupVersion": "discovery.k8s.io/v1",
                        "resources": [
                            {
                                "name": "endpointslices",
                                "singularName": "endpointslice",
                                "namespaced": True,
                                "kind": "EndpointSlice",
                                "verbs": ["get", "list"],
                            }
                        ],
                    }
                )
                return True
            resources = self._crd_resources_for(group, version)
            if not resources:
                return False
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": f"{group}/{version}",
                    "resources": resources,
                }
            )
            return True
        return False

    @classmethod
    def _crd_versions_for_group(cls, group: str) -> list[str]:
        with cls.crd_lock:
            versions = sorted({ver for g, ver, _ in cls.crd_registry.keys() if g == group})
        return versions

    @classmethod
    def _dynamic_group_names(cls) -> list[str]:
        with cls.crd_lock:
            names = sorted({g for (g, _, _) in cls.crd_registry.keys()})
        return names

    @classmethod
    def _crd_resources_for(cls, group: str, version: str) -> list[dict[str, Any]]:
        with cls.crd_lock:
            entries = [
                (plural, meta)
                for (g, v, plural), meta in cls.crd_registry.items()
                if g == group and v == version
            ]
        resources: list[dict[str, Any]] = []
        for plural, meta in entries:
            resources.append(
                {
                    "name": plural,
                    "singularName": meta.get("singularName", ""),
                    "namespaced": meta.get("namespaced", True),
                    "kind": meta.get("kind", ""),
                    "verbs": [
                        "get",
                        "list",
                        "create",
                        "delete",
                        "patch",
                        "update",
                        "watch",
                    ],
                    "shortNames": meta.get("shortNames", []),
                }
            )
        return resources

    @classmethod
    def _register_crd(cls, obj: K8sObject) -> None:
        spec = obj.spec or {}
        group = spec.get("group")
        versions = spec.get("versions", [])
        names = spec.get("names", {})
        plural = names.get("plural")
        kind = names.get("kind")
        scope = spec.get("scope", "Namespaced")
        if not group or not versions or not plural or not kind:
            return
        namespaced = scope.lower() == "namespaced"
        crd_name = obj.name
        with cls.crd_lock:
            cls._unregister_crd(crd_name)
            keys: list[tuple[str, str, str]] = []
            for ver in versions:
                if not ver.get("served", True):
                    continue
                vname = ver.get("name")
                if not vname:
                    continue
                key = (group, vname, plural)
                cls.crd_registry[key] = {
                    "kind": kind,
                    "namespaced": namespaced,
                    "shortNames": names.get("shortNames", []),
                    "singularName": names.get("singular", ""),
                }
                keys.append(key)
            if keys:
                cls.crd_index[crd_name] = keys

    @classmethod
    def _unregister_crd(cls, crd_name: str) -> None:
        with cls.crd_lock:
            keys = cls.crd_index.pop(crd_name, [])
            for key in keys:
                cls.crd_registry.pop(key, None)

    @classmethod
    def _lookup_crd(cls, group: str, version: str, plural: str) -> dict[str, Any] | None:
        with cls.crd_lock:
            return cls.crd_registry.get((group, version, plural))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        # Allow unauthenticated discovery/OpenAPI for kubectl validation
        if path not in {"/openapi/v2", "/swagger.json", "/api", "/apis", "/version"}:
            if not self._authz(role="read"):
                return

        if path == "/healthz" or path == "/readyz":
            self._ok({"status": "ok"})
            return
        if path == "/metrics":
            metrics_txt = ""
            if hasattr(self.server, "store"):  # type: ignore[attr-defined]
                metrics_txt = self.server.store.render_metrics()  # type: ignore[attr-defined]
            data = metrics_txt.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/version":
            self._ok(K8S_VERSION)
            return
        if path == "/api":
            self._ok({"versions": ["v1"]})
            return
        if path == "/apis":
            groups = [
                        {
                            "name": "batch",
                            "versions": [{"groupVersion": "batch/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "batch/v1", "version": "v1"},
                        },
                        {
                            "name": "apps",
                            "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
                        },
                        {
                            "name": "networking.k8s.io",
                            "versions": [
                                {"groupVersion": "networking.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "networking.k8s.io/v1",
                                "version": "v1",
                            },
                        },
                        {
                            "name": "discovery.k8s.io",
                            "versions": [{"groupVersion": "discovery.k8s.io/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "discovery.k8s.io/v1", "version": "v1"},
                        },
                        {
                            "name": "rbac.authorization.k8s.io",
                            "versions": [
                                {"groupVersion": "rbac.authorization.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "rbac.authorization.k8s.io/v1",
                                "version": "v1",
                            },
                        },
                        {
                            "name": "authorization.k8s.io",
                            "versions": [{"groupVersion": "authorization.k8s.io/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "authorization.k8s.io/v1", "version": "v1"},
                        },
                        {
                            "name": "policy",
                            "versions": [{"groupVersion": "policy/v1", "version": "v1"}],
                            "preferredVersion": {"groupVersion": "policy/v1", "version": "v1"},
                        },
                        {
                            "name": "autoscaling",
                            "versions": [
                                {"groupVersion": "autoscaling/v2", "version": "v2"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "autoscaling/v2",
                                "version": "v2",
                            },
                        },
                        {
                            "name": "apiextensions.k8s.io",
                            "versions": [
                                {"groupVersion": "apiextensions.k8s.io/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": "apiextensions.k8s.io/v1",
                                "version": "v1",
                            },
                        },
            ]
            existing = {g["name"] for g in groups}
            for dyn in self._dynamic_group_names():
                if dyn in existing:
                    continue
                versions = self._crd_versions_for_group(dyn)
                if not versions:
                    continue
                groups.append(
                    {
                        "name": dyn,
                        "versions": [
                            {"groupVersion": f"{dyn}/{ver}", "version": ver}
                            for ver in versions
                        ],
                        "preferredVersion": {
                            "groupVersion": f"{dyn}/{versions[0]}",
                            "version": versions[0],
                        },
                    }
                )
            self._ok({"groups": groups})
            return
        if path in ("/openapi/v2", "/swagger.json"):
            self._ok(_swagger_doc())
            return
        if path == "/openapi/v3":
            self._ok(_openapi_v3_stub())
            return
        if path == "/api/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [
                        {
                            "name": "events",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Event",
                            "verbs": ["get", "list", "watch", "create"],
                        },
                        {
                            "name": "namespaces",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "Namespace",
                            "verbs": ["get", "list", "create", "delete", "patch", "update"],
                            "shortNames": ["ns"],
                        },
                        {
                            "name": "configmaps",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ConfigMap",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["cm"],
                        },
                        {
                            "name": "secrets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Secret",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "serviceaccounts",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ServiceAccount",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["sa"],
                        },
                        {
                            "name": "services",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Service",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["svc"],
                        },
                        {
                            "name": "endpoints",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Endpoints",
                            "verbs": ["get", "list"],
                            "shortNames": ["ep"],
                        },
                        {
                            "name": "nodes",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "Node",
                            "verbs": ["get", "list"],
                        },
                        {
                            "name": "pods",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Pod",
                            "verbs": ["get", "list"],
                            "shortNames": ["po"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/apps/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "apps/v1",
                    "resources": [
                        {
                            "name": "statefulsets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "StatefulSet",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["sts"],
                        },
                        {
                            "name": "statefulsets/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "StatefulSet",
                            "verbs": ["get", "patch", "update"],
                        },
                        {
                            "name": "daemonsets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "DaemonSet",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["ds"],
                        },
                        {
                            "name": "daemonsets/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "DaemonSet",
                            "verbs": ["get", "patch", "update"],
                        },
                        {
                            "name": "deployments",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Deployment",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["deploy", "deploys"],
                        },
                        {
                            "name": "deployments/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Deployment",
                            "verbs": ["get", "patch", "update"],
                        },
                        {
                            "name": "deployments/scale",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Scale",
                            "verbs": ["get", "patch", "update"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/batch/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "batch/v1",
                    "resources": [
                        {
                            "name": "jobs",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Job",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "jobs/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Job",
                            "verbs": ["get", "patch", "update"],
                        },
                        {
                            "name": "cronjobs",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "CronJob",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "cronjobs/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "CronJob",
                            "verbs": ["get", "patch", "update"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/networking.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "networking.k8s.io/v1",
                    "resources": [
                        {
                            "name": "ingresses",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Ingress",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["ing"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/rbac.authorization.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "rbac.authorization.k8s.io/v1",
                    "resources": [
                        {
                            "name": "roles",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Role",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "rolebindings",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "RoleBinding",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "clusterroles",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRole",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                        {
                            "name": "clusterrolebindings",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRoleBinding",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                        },
                    ],
                }
            )
            return
        if path == "/apis/authorization.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "authorization.k8s.io/v1",
                    "resources": [
                        {
                            "name": "subjectaccessreviews",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "SubjectAccessReview",
                            "verbs": ["create"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/policy/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "policy/v1",
                    "resources": [
                        {
                            "name": "poddisruptionbudgets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "PodDisruptionBudget",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["pdb"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/autoscaling/v2":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "autoscaling/v2",
                    "resources": [
                        {
                            "name": "horizontalpodautoscalers",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "HorizontalPodAutoscaler",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["hpa"],
                        }
                    ],
                }
            )
            return
        if path == "/apis/apiextensions.k8s.io/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "apiextensions.k8s.io/v1",
                    "resources": [
                        {
                            "name": "customresourcedefinitions",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "CustomResourceDefinition",
                            "verbs": ["get", "list", "create", "delete", "patch", "update", "watch"],
                            "shortNames": ["crd"],
                        }
                    ],
                }
            )
            return

        if self._serve_dynamic_group_discovery(path):
            return

        # Lists and gets for core resources
        plural, ns, name = _ns_name(path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"}:
            if name is None:
                # watch support on LIST endpoints
                if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                    if not self._rbac_allows("watch", plural):
                        self._deny(403)
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        start = time.time()
                        timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                        heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                        allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                        for ev_type, obj in self.server.store.watch("", "v1", plural, ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                            line = json.dumps({"type": ev_type, "object": _to_obj(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                            self.wfile.write(line)
                            self.wfile.flush()
                            if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                break
                    except BrokenPipeError:
                        pass
                    return
                # LIST
                if not self._rbac_allows("list", plural):
                    self._deny(403)
                    return
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                if plural == "namespaces":
                    items = self.server.store.list("", "v1", "namespaces", None)  # type: ignore[attr-defined]
                else:
                    if ns is None:
                        items = self.server.store.list_all("", "v1", plural)  # type: ignore[attr-defined]
                    else:
                        items = self.server.store.list("", "v1", plural, ns)  # type: ignore[attr-defined]
                def _transform(obj: K8sObject) -> dict[str, Any]:
                    if plural == "services":
                        doc = _to_obj(obj)
                        doc = _merge_provider_service(self.server.state, doc, obj)  # type: ignore[attr-defined]
                        return doc
                    return _to_obj(obj)

                self._ok(
                    _list_with_rv(
                        items,
                        _transform,
                        kind=_kind(plural),
                        api_version="v1",
                        limit=limit if limit > 0 else None,
                        continue_token=cont,
                    )
                )
                return
            else:
                # GET
                if not self._rbac_allows("get", plural):
                    self._deny(403)
                    return
                obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                # refresh SA token if missing/expired
                if plural == "serviceaccounts":
                    md = dict(obj.metadata)
                    anns = md.setdefault("annotations", {})
                    tok = anns.get("ae.apishim/token")
                    exp = float(anns.get("ae.apishim/token-exp", "0") or 0)
                    if not tok or exp < time.time():
                        tok = self._issue_sa_token(ns or "default", name)
                        anns["ae.apishim/token"] = tok
                        anns["ae.apishim/token-exp"] = str(int(time.time() + self.sa_token_ttl))
                        obj = self.server.store.upsert(  # type: ignore[attr-defined]
                            "",
                            "v1",
                            plural,
                            None if plural == "namespaces" else ns,
                            name,
                            metadata=md,
                            spec=obj.spec,
                            status=obj.status,
                        )
                if plural == "services":
                    doc = _merge_provider_service(self.server.state, _to_obj(obj), obj)  # type: ignore[attr-defined]
                    self._ok(doc)
                    return
                self._ok(_to_obj(obj))
                return
            # Mutations
            if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                verb = {"POST": "create", "PUT": "update", "PATCH": "patch", "DELETE": "delete"}[self.command]
                if not self._rbac_allows(verb, plural):
                    self._deny(403)
                    return
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"}:
            # Mutations
            pass
        # Endpoints (projected from controller state)
        if plural == "endpoints":
            if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                self.send_response(HTTPStatus.NOT_IMPLEMENTED)
                self.end_headers()
                return
            if name is None:
                # list endpoints within namespace (or all)
                svcs = (
                    self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                    if ns is None
                    else self.server.store.list("", "v1", "services", ns)  # type: ignore[attr-defined]
                )
                items: list[dict[str, Any]] = []
                for svc in svcs:
                    ep = _endpoints_for_service(self.server.state, svc)  # type: ignore[attr-defined]
                    if ep:
                        items.append(ep)
                rv = max((int(i["metadata"].get("resourceVersion", "0")) for i in items), default=0)
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                selected = items
                cont_token = None
                if cont:
                    for idx, obj in enumerate(items):
                        if obj["metadata"].get("name") == cont:
                            selected = items[idx + 1 :]
                            break
                if limit > 0 and len(selected) > limit:
                    cont_token = selected[limit]["metadata"].get("name")
                    selected = selected[:limit]
                meta = {"resourceVersion": str(rv)}
                if cont_token:
                    meta["continue"] = cont_token
                self._ok({"kind": "EndpointsList", "apiVersion": "v1", "metadata": meta, "items": selected})
                return
            svc = self.server.store.get("", "v1", "services", ns, name)  # type: ignore[attr-defined]
            if not svc:
                self._not_found()
                return
            ep = _endpoints_for_service(self.server.state, svc)  # type: ignore[attr-defined]
            if not ep:
                self._not_found()
                return
            self._ok(ep)
            return

        # Pods (projected from runtime containers)
        if plural == "pods":
            containers = []
            try:
                containers = self.server.runtime.list_containers_info()  # type: ignore[attr-defined]
            except Exception:
                containers = []
            label_sel = q.get("labelSelector", [""])[0] or ""

            def _match_labels(labels: dict[str, Any]) -> bool:
                if not label_sel:
                    return True
                for expr in label_sel.split(","):
                    expr = expr.strip()
                    if not expr:
                        continue
                    if "!=" in expr:
                        key, val = expr.split("!=", 1)
                        if labels.get(key) == val:
                            return False
                        continue
                    if "=" in expr:
                        key, val = expr.split("=", 1)
                        if labels.get(key) != val:
                            return False
                        continue
                    # Unsupported selector semantics -> best-effort pass
                return True
            # enrich with controller replica/node info when available
            replica_info: dict[str, tuple[str | None, bool, bool, str, str, str]] = {}
            try:
                # Build once for all apps to avoid N+1 queries
                for app in { (c.get("labels", {}) or {}).get("ae.app") for c in containers }:
                    if not app:
                        continue
                    for rid, node_id, ready, live, status, rmsg, lmsg in self.server.state.list_replica_nodes(app):  # type: ignore[attr-defined]
                        replica_info[rid] = (node_id, ready, live, status, rmsg, lmsg)
            except Exception:
                replica_info = {}
            now_rv = int(time.time() * 1000)
            pod_objs = []
            for c in containers:
                labels = c.get("labels", {}) or {}
                c_ns = labels.get("ae.namespace") or "default"
                if ns and c_ns != ns:
                    continue
                if not _match_labels(labels):
                    continue
                rid = labels.get("ae.replica_id") or c.get("name")
                rep_info = replica_info.get(str(rid))
                node_name = labels.get("ae.node") or (rep_info[0] if rep_info else None)
                pod_obj = _pod_obj(c, now_rv, node_name)
                if rep_info:
                    pod_obj["status"]["hostIP"] = node_name
                    # reflect readiness/live from controller status if available
                    cs = pod_obj["status"]["containerStatuses"][0]
                    cs["ready"] = bool(rep_info[1])
                    pod_obj["status"]["conditions"] = [
                        {"type": "PodScheduled", "status": "True"},
                        {"type": "Ready", "status": "True" if rep_info[1] else "False"},
                        {"type": "ContainersReady", "status": "True" if rep_info[1] else "False"},
                    ]
                    if not rep_info[1]:
                        cs["state"] = {"waiting": {"reason": rep_info[3] or "Pending", "message": rep_info[4]}}
                pod_objs.append(pod_obj)
            if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                if not self._rbac_allows("watch", "pods"):
                    self._deny(403)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for p in pod_objs:
                        ev = {"type": "ADDED", "object": p}
                        self.wfile.write(json.dumps(ev, separators=(",", ":")).encode("utf-8") + b"\n")
                    bm = {
                        "type": "BOOKMARK",
                        "object": {"kind": "Pod", "apiVersion": "v1", "metadata": {"resourceVersion": str(now_rv)}},
                    }
                    self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
                    self.wfile.flush()
                except BrokenPipeError:
                    pass
                return
            if name is None:
                try:
                    limit = int(q.get("limit", ["0"])[0] or 0)
                except Exception:
                    limit = 0
                cont = q.get("continue", [""])[0] or None
                selected = pod_objs
                cont_token = None
                if cont:
                    for idx, obj in enumerate(pod_objs):
                        if obj["metadata"].get("name") == cont:
                            selected = pod_objs[idx + 1 :]
                            break
                if limit > 0 and len(selected) > limit:
                    cont_token = selected[limit]["metadata"].get("name")
                    selected = selected[:limit]
                meta = {"resourceVersion": str(now_rv)}
                if cont_token:
                    meta["continue"] = cont_token
                self._ok({"kind": "PodList", "apiVersion": "v1", "metadata": meta, "items": selected})
                return
            for p in pod_objs:
                if p["metadata"]["name"] == name:
                    self._ok(p)
                    return
            self._not_found()
            return
        # Pod logs (text; supports follow/tail/timestamps)
        m_logs = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/log$", path)
        if m_logs:
            ns, pod_name = m_logs.group(1), m_logs.group(2)
            tail = q.get("tailLines", ["100"])[0]
            follow = q.get("follow", ["false"])[0].lower() in ("1", "true", "yes")
            timestamps = q.get("timestamps", ["false"])[0].lower() in ("1", "true", "yes")
            since = q.get("sinceSeconds", [None])[0]
            since_i: int | None
            try:
                tail_i = int(tail)
            except Exception:
                tail_i = 100
            try:
                since_i = int(since) if since is not None else None
            except Exception:
                since_i = None

            def _emit(line: str) -> bytes:
                if timestamps:
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    line_ts = f"{now} {line}"
                else:
                    line_ts = line
                return line_ts.encode("utf-8", errors="ignore")

            try:
                if follow:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    for ln in self.server.runtime.read_logs(pod_name, follow=True, tail=tail_i, since=since_i):  # type: ignore[attr-defined]
                        try:
                            data = _emit(ln)
                            if not data.endswith(b"\n"):
                                data += b"\n"
                            self.wfile.write(data)
                            self.wfile.flush()
                        except BrokenPipeError:
                            break
                    return
                else:
                    lines = list(self.server.runtime.read_logs(pod_name, follow=False, tail=tail_i, since=since_i))  # type: ignore[attr-defined]
                    body = b"".join([_emit(ln) for ln in lines])
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except Exception as exc:
                self._json_status(HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc))
                return
        # Pod exec (kubectl uses SPDY upgrade)
        m_exec = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/exec$", path)
        if m_exec:
            if not self._rbac_allows("create", "pods/exec"):
                self._deny(403)
                return
            qs = parse_qs(parsed.query)
            cmd = qs.get("command") or []
            if not cmd:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="command query param is required")
                return
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                container = (qs.get("container") or [None])[0]
                tty = (qs.get("tty") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdin = (qs.get("stdin") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdout = (qs.get("stdout") or ["true"])[0].lower() in ("1", "true", "yes")
                want_stderr = (qs.get("stderr") or ["true"])[0].lower() in ("1", "true", "yes")
                self._handle_exec_spdy(
                    pod_name=m_exec.group(2),
                    command=list(cmd),
                    container=container,
                    tty=tty,
                    want_stdin=want_stdin,
                    want_stdout=want_stdout,
                    want_stderr=want_stderr,
                )
                return
            elif upgrade == "websocket":
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="kubectl exec requires SPDY/3.1; websocket exec not implemented",
                )
                return
            else:
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="exec requires SPDY/3.1 upgrade used by kubectl",
                )
                return
        # Service port-forward
        m_pf_svc = re.match(r"^/api/v1/namespaces/([^/]+)/services/([^/]+)/portforward$", path)
        if m_pf_svc:
            if not self._rbac_allows("create", "services/portforward"):
                self._deny(403)
                return
            ns = m_pf_svc.group(1)
            svc_name = m_pf_svc.group(2)
            svc = self.server.store.get("", "v1", "services", ns, svc_name)  # type: ignore[attr-defined]
            if not svc:
                self._not_found()
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            svc_ports = svc.spec.get("ports", []) if svc.spec else []

            def _resolve_port(pval: str) -> int | None:
                for sp in svc_ports:
                    if str(sp.get("port")) == pval or sp.get("name") == pval:
                        tp = sp.get("targetPort", sp.get("port"))
                        if isinstance(tp, int):
                            return tp
                        try:
                            return int(tp)
                        except Exception:
                            try:
                                return int(sp.get("port"))
                            except Exception:
                                return None
                try:
                    return int(pval)
                except Exception:
                    return None

            target_ports: list[int] = []
            for p in ports_q:
                rp = _resolve_port(p)
                if rp:
                    target_ports.append(rp)
            if not target_ports and svc_ports:
                # default to first port/targetPort
                tp = svc_ports[0].get("targetPort", svc_ports[0].get("port"))
                try:
                    target_ports.append(int(tp))
                except Exception:
                    try:
                        target_ports.append(int(svc_ports[0].get("port")))
                    except Exception:
                        pass
            if not target_ports:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="ports query param required")
                return
            app_name = _service_app_name(svc)
            eps_raw = self.server.state.list_service_endpoints(app_name) if app_name else []  # type: ignore[attr-defined]
            target_ip = _pick_endpoint_ip(eps_raw, key=",".join(str(p) for p in target_ports) if target_ports else None)
            if not target_ip and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_ip = os.getenv("AE_STUB_BACKEND_HOST", "127.0.0.1")
                if not target_ports:
                    try:
                        target_ports = [int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))]
                    except Exception:
                        target_ports = [8081]
            if not target_ip:
                self._json_status(HTTPStatus.SERVICE_UNAVAILABLE, reason="NoEndpoints", message="no ready endpoints for service")
                return
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                # choose endpoint per target port to spread load
                ep_map: dict[int, list[str]] = {}
                for tp in target_ports:
                    # include all ready endpoints for port spread
                    port_ips = [ep.ip for ep in eps_raw if ep.ready] or [ep.ip for ep in eps_raw]
                    if port_ips:
                        ep_map[tp] = port_ips
                # fallback: single target_ip if map empty
                self._handle_port_forward_spdy(target_ip, target_ports, ep_map if ep_map else None)
            elif upgrade == "websocket":
                self._handle_port_forward_ws(target_ip, target_ports[0])
            else:
                self._json_status(HTTPStatus.UPGRADE_REQUIRED, reason="UpgradeRequired", message="port-forward requires SPDY/3.1 used by kubectl")
            return
        # Port-forward
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", path)
        if m_pf:
            if not self._rbac_allows("create", "pods/portforward"):
                self._deny(403)
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            pod_name = m_pf.group(2)
            target_host = "127.0.0.1"
            container_info = None
            try:
                for c in self.server.runtime.list_containers_info():  # type: ignore[attr-defined]
                    labels = c.get("labels", {}) or {}
                    if labels.get("ae.replica_id") == pod_name or c.get("name") == pod_name:
                        container_info = c
                        break
            except Exception:
                container_info = None
            if container_info:
                target_host = (
                    container_info.get("pod_ip")
                    or container_info.get("host_ip")
                    or container_info.get("hostIP")
                    or target_host
                )
            upgrade = (self.headers.get("Upgrade") or "").lower()
            target_ports: list[int] = []
            for p in ports_q:
                try:
                    target_ports.append(int(p))
                except Exception:
                    pass
            if container_info:
                try:
                    hp = container_info.get("host_ports") or container_info.get("hostPorts") or []
                    if hp:
                        target_ports = [int(hp[0])]
                except Exception:
                    pass
            if not target_ports and container_info:
                try:
                    hp = container_info.get("host_ports") or container_info.get("hostPorts") or []
                    if hp:
                        target_ports.append(int(hp[0]))
                except Exception:
                    pass
            if not target_ports and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                try:
                    target_ports.append(int(os.getenv("AE_STUB_BACKEND_PORT", "8081")))
                    target_host = os.getenv("AE_STUB_BACKEND_HOST", target_host)
                except Exception:
                    pass
            if not target_ports:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="ports query param required")
                return
            if upgrade == "websocket":
                self._handle_port_forward_ws(target_host, target_ports[0])
            elif upgrade.startswith("spdy"):
                self._handle_port_forward_spdy(target_host, target_ports)
            else:
                self._json_status(HTTPStatus.UPGRADE_REQUIRED, reason="UpgradeRequired", message="port-forward requires SPDY/3.1 used by kubectl")
            return
        # Events (lightweight list/watch sourced from controller events, empty fallback)
        m_events = re.match(r"^/api/v1(?:/namespaces/([^/]+))?/events(?:/([^/]+))?$", path)
        if m_events:
            ns = m_events.group(1)
            name = m_events.group(2)
            if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                # No persisted watch stream yet; emit bookmark-only
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    rv = int(time.time() * 1000)
                    line = json.dumps(
                        {
                            "type": "BOOKMARK",
                            "object": {"kind": "Event", "apiVersion": "v1", "metadata": {"resourceVersion": str(rv)}},
                        },
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                    self.wfile.write(line)
                    self.wfile.flush()
                except BrokenPipeError:
                    pass
                return
            # best-effort pull from controller events by namespace
            items = []
            try:
                deps = (
                    self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                    if ns is None
                    else self.server.store.list("apps", "v1", "deployments", ns)  # type: ignore[attr-defined]
                )
                # include jobs for batch events
                jobs = (
                    self.server.store.list_all("batch", "v1", "jobs")  # type: ignore[attr-defined]
                    if ns is None
                    else self.server.store.list("batch", "v1", "jobs", ns)  # type: ignore[attr-defined]
                )
                for dep in deps + jobs:
                    app_name = _app_name(dep.namespace, dep.name)
                    for ev in self.server.state.list_events(app_name, limit=50):  # type: ignore[attr-defined]
                        if name and ev.message != name:
                            continue
                        items.append(_to_event(dep.namespace or "default", dep.name, ev))
            except Exception:
                items = []
            rv = int(time.time() * 1000)
            self._ok({"kind": "EventList", "apiVersion": "v1", "metadata": {"resourceVersion": str(rv)}, "items": items})
            return

        # Nodes (projected from controller state)
        if path == "/api/v1/nodes":
            nodes = self.server.state.list_nodes()  # type: ignore[attr-defined]
            now_rv = int(time.time() * 1000)
            items = []
            for idx, (rec, st) in enumerate(nodes, start=1):
                items.append(_node_obj(rec, st, now_rv + idx))
            self._ok({"kind": "NodeList", "apiVersion": "v1", "metadata": {"resourceVersion": str(now_rv)}, "items": items})
            return
        if path.startswith("/api/v1/nodes/"):
            node_name = path.split("/")[-1]
            nodes = self.server.state.list_nodes()  # type: ignore[attr-defined]
            for rec, st in nodes:
                if node_name in {rec.node_id, rec.name or ""}:
                    self._ok(_node_obj(rec, st, int(time.time() * 1000)))
                    return
            self._not_found()
            return

        # apps/v1 deployments
        if path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(path)
            if d_plural == "deployments":
                if d_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "deployments"):
                            self._deny(403)
                            return
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                            for ev_type, obj in self.server.store.watch("apps", "v1", "deployments", d_ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                                line = json.dumps({"type": ev_type, "object": _to_deployment(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                                self.wfile.write(line)
                                self.wfile.flush()
                                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                    break
                        except BrokenPipeError:
                            pass
                        return
                    if not self._rbac_allows("list", "deployments"):
                        self._deny(403)
                        return
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    items = (
                        self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "deployments", d_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        _list_with_rv(items, _to_deployment, kind="Deployment", api_version="apps/v1", limit=limit if limit > 0 else None, continue_token=cont)
                    )
                    return
                else:
                    if not self._rbac_allows("get", "deployments"):
                        self._deny(403)
                        return
                    obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_deployment(obj))
                    return
            if d_plural == "deployments/status" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_deployment(obj))
                return
            if d_plural == "statefulsets":
                transform = _to_statefulset
                if d_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "statefulsets"):
                            self._deny(403)
                            return
                        self._stream_watch("apps", "v1", "statefulsets", d_ns, q, transform=transform)
                        return
                    if not self._rbac_allows("list", "statefulsets"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("apps", "v1", "statefulsets")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "statefulsets", d_ns)  # type: ignore[attr-defined]
                    )
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    self._ok(_list_with_rv(items, transform, kind="StatefulSet", api_version="apps/v1", limit=limit if limit > 0 else None, continue_token=cont))
                    return
                else:
                    if not self._rbac_allows("get", "statefulsets"):
                        self._deny(403)
                        return
                    obj = self.server.store.get("apps", "v1", "statefulsets", d_ns, d_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(transform(obj))
                    return
            if d_plural == "statefulsets/status" and d_name:
                obj = self.server.store.get("apps", "v1", "statefulsets", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_statefulset(obj))
                return
            if d_plural == "daemonsets":
                if d_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "daemonsets"):
                            self._deny(403)
                            return
                        self._stream_watch("apps", "v1", "daemonsets", d_ns, q, transform=lambda o: _to_daemonset(o))
                        return
                    if not self._rbac_allows("list", "daemonsets"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("apps", "v1", "daemonsets")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "daemonsets", d_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(_list_with_rv(items, lambda o: _to_daemonset(o), kind="DaemonSet", api_version="apps/v1"))
                    return
                else:
                    if not self._rbac_allows("get", "daemonsets"):
                        self._deny(403)
                        return
                    obj = self.server.store.get("apps", "v1", "daemonsets", d_ns, d_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    desired = None
                    try:
                        desired = len([n for n, _ in self.server.state.list_nodes()])  # type: ignore[attr-defined]
                    except Exception:
                        desired = None
                    self._ok(_to_daemonset(obj, desired=desired))
                    return
            if d_plural == "daemonsets/status" and d_name:
                obj = self.server.store.get("apps", "v1", "daemonsets", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_daemonset(obj))
                return
            if d_plural == "deployments/scale" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_scale(obj))
                return

        # networking ingresses
        if path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(path)
            if n_plural == "ingresses":
                if n_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "ingresses"):
                            self._deny(403)
                            return
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"]) [0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"]) [0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"]) [0] in ("1", "true", "True")
                            for ev_type, obj in self.server.store.watch("networking.k8s.io", "v1", "ingresses", n_ns, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                                line = json.dumps({"type": ev_type, "object": _to_ingress(obj, self.server.state)}, separators=(",", ":")).encode("utf-8") + b"\n"  # type: ignore[attr-defined]
                                self.wfile.write(line)
                                self.wfile.flush()
                                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                    break
                        except BrokenPipeError:
                            pass
                        return
                    if not self._rbac_allows("list", "ingresses"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("networking.k8s.io", "v1", "ingresses")  # type: ignore[attr-defined]
                        if n_ns is None
                        else self.server.store.list("networking.k8s.io", "v1", "ingresses", n_ns)  # type: ignore[attr-defined]
                    )
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    self._ok(_list_with_rv(items, lambda o: _to_ingress(o, self.server.state), kind="Ingress", api_version="networking.k8s.io/v1", limit=limit if limit > 0 else None, continue_token=cont))  # type: ignore[attr-defined]
                    return
                else:
                    if not self._rbac_allows("get", "ingresses"):
                        self._deny(403)
                        return
                    obj = self.server.store.get("networking.k8s.io", "v1", "ingresses", n_ns, n_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_ingress(obj, self.server.state))  # type: ignore[attr-defined]
                    return

        # batch/v1 Jobs and CronJobs (stored passthrough with synthesized status)
        if path.startswith("/apis/batch/v1"):
            b_plural, b_ns, b_name = _batch_ns_name(path)
            if b_plural == "jobs":
                transform = _to_job
                if b_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "jobs"):
                            self._deny(403)
                            return
                        self._stream_watch("batch", "v1", "jobs", b_ns, q, transform=transform)
                        return
                    if not self._rbac_allows("list", "jobs"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("batch", "v1", "jobs")  # type: ignore[attr-defined]
                        if b_ns is None
                        else self.server.store.list("batch", "v1", "jobs", b_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(_list_with_rv(items, transform, kind="Job", api_version="batch/v1"))
                    return
                obj = self.server.store.get("batch", "v1", "jobs", b_ns, b_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(transform(obj))
                return
            if b_plural == "jobs/status" and b_name:
                obj = self.server.store.get("batch", "v1", "jobs", b_ns, b_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_job(obj))
                return
            if b_plural == "cronjobs":
                transform = _to_cronjob
                if b_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "cronjobs"):
                            self._deny(403)
                            return
                        self._stream_watch("batch", "v1", "cronjobs", b_ns, q, transform=transform)
                        return
                    if not self._rbac_allows("list", "cronjobs"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("batch", "v1", "cronjobs")  # type: ignore[attr-defined]
                        if b_ns is None
                        else self.server.store.list("batch", "v1", "cronjobs", b_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(_list_with_rv(items, transform, kind="CronJob", api_version="batch/v1"))
                    return
                obj = self.server.store.get("batch", "v1", "cronjobs", b_ns, b_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(transform(obj))
                return
            if b_plural == "cronjobs/status" and b_name:
                obj = self.server.store.get("batch", "v1", "cronjobs", b_ns, b_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_cronjob(obj))
                return

        # discovery.k8s.io EndpointSlice (projected)
        if path.startswith("/apis/discovery.k8s.io/v1"):
            e_plural, e_ns, e_name = _gv_ns_name(path, "discovery.k8s.io", "v1", "endpointslices")
            if e_plural == "endpointslices":
                if e_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "endpointslices"):
                            self._deny(403)
                            return
                        # We don't persist slices; emulate watch with list + bookmark
                        items = []
                        svcs = (
                            self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                            if e_ns is None
                            else self.server.store.list("", "v1", "services", e_ns)  # type: ignore[attr-defined]
                        )
                        for svc in svcs:
                            eps = _endpointslice_for_service(self.server.state, svc)  # type: ignore[attr-defined]
                            if eps:
                                items.append(eps)
                        self._stream_fake_watch(items, kind="EndpointSlice", api_version="discovery.k8s.io/v1")
                        return
                    if not self._rbac_allows("list", "endpointslices"):
                        self._deny(403)
                        return
                    svcs = (
                        self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                        if e_ns is None
                        else self.server.store.list("", "v1", "services", e_ns)  # type: ignore[attr-defined]
                    )
                    items = []
                    for svc in svcs:
                        eps = _endpointslice_for_service(self.server.state, svc)  # type: ignore[attr-defined]
                        if eps:
                            items.append(eps)
                    rv = max((int(i["metadata"].get("resourceVersion", "0")) for i in items), default=0)
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    selected = items
                    cont_token = None
                    if cont:
                        for idx, obj in enumerate(items):
                            if obj["metadata"].get("name") == cont:
                                selected = items[idx + 1 :]
                                break
                    if limit > 0 and len(selected) > limit:
                        cont_token = selected[limit]["metadata"].get("name")
                        selected = selected[:limit]
                    meta = {"resourceVersion": str(rv)}
                    if cont_token:
                        meta["continue"] = cont_token
                    self._ok({"kind": "EndpointSliceList", "apiVersion": "discovery.k8s.io/v1", "metadata": meta, "items": selected})
                    return
                if not self._rbac_allows("get", "endpointslices"):
                    self._deny(403)
                    return
                svc_name = None
                if "-" in e_name:
                    svc_name = "-".join(e_name.split("-")[:-1]) or None
                if not svc_name:
                    svc_name = e_name
                svc = self.server.store.get("", "v1", "services", e_ns, svc_name)  # type: ignore[attr-defined]
                if not svc:
                    self._not_found()
                    return
                eps = _endpointslice_for_service(self.server.state, svc)  # type: ignore[attr-defined]
                if not eps:
                    self._not_found()
                    return
                self._ok(eps)
                return

        # apiextensions CRDs
        if path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions":
                if crd_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch(
                            "apiextensions.k8s.io",
                            "v1",
                            "customresourcedefinitions",
                            None,
                            q,
                            transform=_to_crd,
                        )
                        return
                    items = self.server.store.list_all(  # type: ignore[attr-defined]
                        "apiextensions.k8s.io", "v1", "customresourcedefinitions"
                    )
                    self._ok(
                        {
                            "kind": "CustomResourceDefinitionList",
                            "apiVersion": "apiextensions.k8s.io/v1",
                            "items": [_to_crd(i) for i in items],
                        }
                    )
                    return
                obj = self.server.store.get(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io", "v1", "customresourcedefinitions", None, crd_name
                )
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_crd(obj))
                return

        if self._handle_custom_resource_get(path, q):
            return

        # rbac: roles/rolebindings (namespaced) and clusterroles/clusterrolebindings (cluster-scoped)
        if path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced
            r_plural, r_ns, r_name = _gv_ns_name(path, "rbac.authorization.k8s.io", "v1", "roles")
            if r_plural == "roles":
                if r_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "roles", r_ns, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles"))
                        return
                    items = (
                        self.server.store.list_all("rbac.authorization.k8s.io", "v1", "roles")  # type: ignore[attr-defined]
                        if r_ns is None
                        else self.server.store.list("rbac.authorization.k8s.io", "v1", "roles", r_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "RoleList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "roles", r_ns, r_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(obj))
                return
            rb_plural, rb_ns, rb_name = _gv_ns_name(path, "rbac.authorization.k8s.io", "v1", "rolebindings")
            if rb_plural == "rolebindings":
                if rb_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings"))
                        return
                    items = (
                        self.server.store.list_all("rbac.authorization.k8s.io", "v1", "rolebindings")  # type: ignore[attr-defined]
                        if rb_ns is None
                        else self.server.store.list("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "RoleBindingList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns, rb_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings")(obj))
                return
            # cluster-scoped
            cr_plural, cr_name = _gv_cluster_name(path, "rbac.authorization.k8s.io", "v1", "clusterroles")
            if cr_plural == "clusterroles":
                if cr_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "clusterroles", None, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles"))
                        return
                    items = self.server.store.list_all("rbac.authorization.k8s.io", "v1", "clusterroles")  # type: ignore[attr-defined]
                    self._ok({"kind": "ClusterRoleList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "clusterroles", None, cr_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles")(obj))
                return
            crb_plural, crb_name = _gv_cluster_name(path, "rbac.authorization.k8s.io", "v1", "clusterrolebindings")
            if crb_plural == "clusterrolebindings":
                if crb_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, q, transform=_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings"))
                        return
                    items = self.server.store.list_all("rbac.authorization.k8s.io", "v1", "clusterrolebindings")  # type: ignore[attr-defined]
                    self._ok({"kind": "ClusterRoleBindingList", "apiVersion": "rbac.authorization.k8s.io/v1", "items": [_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings")(i) for i in items]})
                    return
                obj = self.server.store.get("rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, crb_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "ClusterRoleBinding", "clusterrolebindings")(obj))
                return

        # policy/v1 PodDisruptionBudget
        if path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets":
                if p_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("policy", "v1", "poddisruptionbudgets", p_ns, q, transform=_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets"))
                        return
                    items = (
                        self.server.store.list_all("policy", "v1", "poddisruptionbudgets")  # type: ignore[attr-defined]
                        if p_ns is None
                        else self.server.store.list("policy", "v1", "poddisruptionbudgets", p_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "PodDisruptionBudgetList", "apiVersion": "policy/v1", "items": [_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(i) for i in items]})
                    return
                obj = self.server.store.get("policy", "v1", "poddisruptionbudgets", p_ns, p_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(obj))
                return

        # autoscaling/v2 HPA
        if path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers":
                if h_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
                        self._stream_watch("autoscaling", "v2", "horizontalpodautoscalers", h_ns, q, transform=lambda o: _to_hpa(o, self.server.store))  # type: ignore[attr-defined]
                        return
                    items = (
                        self.server.store.list_all("autoscaling", "v2", "horizontalpodautoscalers")  # type: ignore[attr-defined]
                        if h_ns is None
                        else self.server.store.list("autoscaling", "v2", "horizontalpodautoscalers", h_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "HorizontalPodAutoscalerList", "apiVersion": "autoscaling/v2", "items": [_to_hpa(i, self.server.store) for i in items]})  # type: ignore[attr-defined]
                    return
                obj = self.server.store.get("autoscaling", "v2", "horizontalpodautoscalers", h_ns, h_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_hpa(obj, self.server.store))  # type: ignore[attr-defined]
                return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        # SubjectAccessReview should be callable by read tokens; other POSTs require write/admin.
        if path.startswith("/apis/authorization.k8s.io/"):
            if not self._authz(role="read"):
                return
        else:
            if not self._authz(role="write"):
                return
        body = self._read_body()
        doc = _read_json(body)

        if path.startswith("/apis/authorization.k8s.io/v1/subjectaccessreviews"):
            status = self._eval_subject_access_review(doc.get("spec") or {})
            resp = {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SubjectAccessReview",
                "spec": doc.get("spec") or {},
                "status": status,
            }
            out = _json(resp)
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        # Pod exec (JSON {command:[], timeoutSeconds?})
        m_exec = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/exec$", path)
        if m_exec:
            cmd = doc.get("command") or doc.get("cmd")
            timeout = doc.get("timeoutSeconds") or doc.get("timeout")
            if not isinstance(cmd, list) or not cmd:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="command must be a non-empty list")
                return
            try:
                rc = int(
                    self.server.runtime.exec(  # type: ignore[attr-defined]
                        m_exec.group(2), [str(c) for c in cmd], timeout=int(timeout) if timeout else None
                    )
                )
                self._ok({"kind": "Status", "status": "Success", "code": 200, "metadata": {}, "details": {"exitCode": rc}})
            except Exception as exc:
                self._json_status(HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc))
            return
        # Pod port-forward (kubectl uses POST + SPDY upgrade)
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", path)
        if m_pf:
            if not self._rbac_allows("create", "pods/portforward"):
                self._deny(403)
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            pod_name = m_pf.group(2)
            container_info = None
            try:
                for c in self.server.runtime.list_containers_info():  # type: ignore[attr-defined]
                    labels = c.get("labels", {}) or {}
                    if labels.get("ae.replica_id") == pod_name or c.get("name") == pod_name:
                        container_info = c
                        break
            except Exception:
                container_info = None
            target_ports: list[int] = []
            for p in ports_q:
                try:
                    target_ports.append(int(p))
                except Exception:
                    pass
            if container_info:
                try:
                    hp = container_info.get("host_ports") or container_info.get("hostPorts") or []
                    if hp:
                        target_ports = [int(hp[0])]
                except Exception:
                    pass
            if not target_ports and container_info:
                try:
                    hp = container_info.get("host_ports") or container_info.get("hostPorts") or []
                    if hp:
                        target_ports.append(int(hp[0]))
                except Exception:
                    pass
            if not target_ports and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                try:
                    target_ports.append(int(os.getenv("AE_STUB_BACKEND_PORT", "8081")))
                except Exception:
                    target_ports.append(8081)
            if not target_ports:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="ports query param required")
                return
            target_host = "127.0.0.1"
            if container_info:
                target_host = (
                    container_info.get("pod_ip")
                    or container_info.get("host_ip")
                    or container_info.get("hostIP")
                    or target_host
                )
            elif isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_host = os.getenv("AE_STUB_BACKEND_HOST", target_host)
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                self._handle_port_forward_spdy(target_host, target_ports)
            elif upgrade == "websocket":
                self._handle_port_forward_ws(target_host, target_ports[0])
            else:
                self._json_status(HTTPStatus.UPGRADE_REQUIRED, reason="UpgradeRequired", message="port-forward requires SPDY/3.1 used by kubectl")
            return
        # Service port-forward POST
        m_pf_svc = re.match(r"^/api/v1/namespaces/([^/]+)/services/([^/]+)/portforward$", path)
        if m_pf_svc:
            if not self._rbac_allows("create", "services/portforward"):
                self._deny(403)
                return
            ns = m_pf_svc.group(1)
            svc_name = m_pf_svc.group(2)
            svc = self.server.store.get("", "v1", "services", ns, svc_name)  # type: ignore[attr-defined]
            if not svc:
                self._not_found()
                return
            ep = _endpoints_for_service(self.server.state, svc)  # type: ignore[attr-defined]
            subsets = (ep or {}).get("subsets") or []
            addresses = subsets[0].get("addresses") if subsets else None
            target_ip = (addresses[0].get("ip") if addresses else None) if addresses else None
            if not target_ip and subsets:
                nr = subsets[0].get("notReadyAddresses") or []
                target_ip = (nr[0].get("ip") if nr else None) if nr else None
            if not target_ip and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_ip = os.getenv("AE_STUB_BACKEND_HOST", "127.0.0.1")
            if not target_ip:
                self._json_status(HTTPStatus.SERVICE_UNAVAILABLE, reason="NoEndpoints", message="no ready endpoints for service")
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            svc_ports = svc.spec.get("ports", []) if svc.spec else []

            def _resolve_port(pval: str) -> int | None:
                for sp in svc_ports:
                    if str(sp.get("port")) == pval or sp.get("name") == pval:
                        tp = sp.get("targetPort", sp.get("port"))
                        if isinstance(tp, int):
                            return tp
                        try:
                            return int(tp)
                        except Exception:
                            try:
                                return int(sp.get("port"))
                            except Exception:
                                return None
                try:
                    return int(pval)
                except Exception:
                    return None

            target_ports: list[int] = []
            for p in ports_q:
                rp = _resolve_port(p)
                if rp:
                    target_ports.append(rp)
            if not target_ports and svc_ports:
                tp = svc_ports[0].get("targetPort", svc_ports[0].get("port"))
                try:
                    target_ports.append(int(tp))
                except Exception:
                    try:
                        target_ports.append(int(svc_ports[0].get("port")))
                    except Exception:
                        pass
            if not target_ports and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                try:
                    target_ports.append(int(os.getenv("AE_STUB_BACKEND_PORT", "8081")))
                except Exception:
                    target_ports.append(8081)
            if not target_ports:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="ports query param required")
                return
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                self._handle_port_forward_spdy(target_ip, target_ports)
            elif upgrade == "websocket":
                self._handle_port_forward_ws(target_ip, target_ports[0])
            else:
                self._json_status(HTTPStatus.UPGRADE_REQUIRED, reason="UpgradeRequired", message="port-forward requires SPDY/3.1 used by kubectl")
            return

        plural, ns, name = _ns_name(path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"}:
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            if not isinstance(name_in, str) or not name_in:
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="metadata.name required")
                return
            ns_in = md.get("namespace") or ns
            if plural == "namespaces":
                ns_in = None
            if not _valid_name(name_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            spec_in = doc.get("data") if plural in {"configmaps", "secrets"} else (doc.get("spec") or {})
            status_in = doc.get("status") or {}
            # Service enrichments: allocate clusterIP/nodePort if missing and validate collisions
            if plural == "serviceaccounts":
                annotations = md.setdefault("annotations", {})
                token = self._issue_sa_token(ns_in or "default", name_in)
                annotations.setdefault("ae.apishim/token", token)
                annotations.setdefault("ae.apishim/token-exp", str(int(time.time() + self.sa_token_ttl)))
            if plural == "services":
                spec_in = dict(spec_in or {})
                existing_svcs = self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                existing_cluster_ips = {s.spec.get("clusterIP") for s in existing_svcs if s.spec.get("clusterIP")}
                # include provider allocations to avoid clashes across restart
                try:
                    existing_cluster_ips |= {s.cluster_ip for s in self.server.state.list_services()}  # type: ignore[attr-defined]
                except Exception:
                    pass
                existing_nodeports = set()
                for s in existing_svcs:
                    for p in s.spec.get("ports", []) if s.spec else []:
                        npv = p.get("nodePort")
                        if npv is not None:
                            try:
                                existing_nodeports.add(int(npv))
                            except Exception:
                                pass
                # include controller-recorded nodePorts
                try:
                    for srec in self.server.state.list_services():  # type: ignore[attr-defined]
                        for _, port_cfg in (srec.ports or {}).items():
                            npv = port_cfg.get("nodePort")
                            if npv is not None:
                                try:
                                    existing_nodeports.add(int(npv))
                                except Exception:
                                    pass
                except Exception:
                    pass
                svc_type = (spec_in.get("type") or "ClusterIP") or "ClusterIP"
                # clusterIP allocation unless headless/ExternalName
                if svc_type != "ExternalName" and spec_in.get("clusterIP") not in {"None", None}:
                    if spec_in.get("clusterIP") is None:
                        spec_in["clusterIP"] = _alloc_cluster_ip(ns_in, name_in, existing_cluster_ips)
                    else:
                        cip = str(spec_in.get("clusterIP"))
                        if cip in existing_cluster_ips:
                            self._json_status(HTTPStatus.CONFLICT, reason="AlreadyExists", message=f"clusterIP {cip} already allocated")
                            return
                # nodePort allocation for NodePort/LoadBalancer
                if svc_type in {"NodePort", "LoadBalancer"}:
                    ports = spec_in.get("ports") or []
                    new_ports = []
                    for p in ports:
                        p = dict(p)
                        np = p.get("nodePort")
                        if np is not None:
                            try:
                                np_i = int(np)
                            except Exception:
                                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="nodePort must be integer")
                                return
                            if np_i in existing_nodeports:
                                self._json_status(HTTPStatus.CONFLICT, reason="AlreadyExists", message=f"nodePort {np_i} already allocated")
                                return
                            existing_nodeports.add(np_i)
                            p["nodePort"] = np_i
                        else:
                            np_alloc = _alloc_nodeport(existing_nodeports, f"{ns_in}/{name_in}/{p.get('name')}/{p.get('port')}")
                            p["nodePort"] = np_alloc
                            existing_nodeports.add(np_alloc)
                        new_ports.append(p)
                    spec_in["ports"] = new_ports
                status_in = status_in or {}
                if svc_type in {"LoadBalancer", "NodePort"}:
                    if "status" not in status_in or not status_in:
                        status_in = {"loadBalancer": {"ingress": []}}
                status_in = _service_lb_status(spec_in, status_in, None)  # provider IP may not exist yet during create
            created = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=spec_in,
                status=status_in,
            )
            self.send_response(HTTPStatus.CREATED)
            out = _json(_to_obj(created))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        # apps/v1 deployments
        if path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(path)
            if d_plural == "deployments":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                spec_in = doc.get("spec") or {}
                spec_in = _inject_sa_projection(spec_in)
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=spec_in,
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_deployment(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if d_plural == "statefulsets":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                spec_in = _inject_sa_projection(doc.get("spec") or {})
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "statefulsets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "statefulsets"),
                    spec=spec_in,
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_statefulset(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if d_plural == "daemonsets":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                spec_in = _inject_sa_projection(doc.get("spec") or {})
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "daemonsets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "daemonsets"),
                    spec=spec_in,
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_daemonset(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
        # networking.k8s.io/v1 ingresses
        if path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(path)
            if n_plural == "ingresses":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or n_name
                ns_in = md.get("namespace") or n_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "networking.k8s.io",
                    "v1",
                    "ingresses",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "ingresses"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_ingress(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # batch/v1 jobs/cronjobs
        if path.startswith("/apis/batch/v1"):
            b_plural, b_ns, b_name = _batch_ns_name(path)
            if b_plural in {"jobs", "cronjobs"}:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or b_name
                ns_in = md.get("namespace") or b_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                spec_in = _inject_sa_projection(doc.get("spec") or {})
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "batch",
                    "v1",
                    b_plural,
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, b_plural),
                    spec=spec_in,
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_job(created) if b_plural == "jobs" else _to_cronjob(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # CRDs
        if path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or crd_name
                if not name_in or not _valid_name(name_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io",
                    "v1",
                    "customresourcedefinitions",
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, "customresourcedefinitions"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._register_crd(created)
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_crd(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        if self._handle_custom_resource_post(doc):
            return

        # rbac (namespaced and cluster resources)
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced roles/rolebindings
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if r_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or r_name
                    ns_in = md.get("namespace") or r_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self.send_response(HTTPStatus.CREATED)
                    out = _json(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
            # clusterroles/clusterrolebindings
            for plural, kind in ("clusterroles", "ClusterRole"), ("clusterrolebindings", "ClusterRoleBinding"):
                cr_plural, cr_name = _gv_cluster_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if cr_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or cr_name
                    if not name_in or not _valid_name(name_in):
                        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self.send_response(HTTPStatus.CREATED)
                    out = _json(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return

        # policy/v1 PDB
        if self.path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(self.path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or p_name
                ns_in = md.get("namespace") or p_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "policy",
                    "v1",
                    "poddisruptionbudgets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "poddisruptionbudgets"),
                    spec=_spec_payload("poddisruptionbudgets", doc),
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # autoscaling/v2 HPA
        if path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or h_name
                ns_in = md.get("namespace") or h_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata")
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "autoscaling",
                    "v2",
                    "horizontalpodautoscalers",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "horizontalpodautoscalers"),
                    spec=_spec_payload("horizontalpodautoscalers", doc),
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        self._not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authz():
            return
        body = self._read_body()
        doc = _read_json(body)
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            ns_in = md.get("namespace") or ns
            if plural == "namespaces":
                ns_in = None
            if not _valid_name(name_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            updated = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=doc.get("data") if plural in {"configmaps", "secrets"} else (doc.get("spec") or {}),
                status=doc.get("status") or {},
            )
            self._ok(_to_obj(updated))
            return
        # apps/v1 deployments and networking ingresses
        if self.path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(self.path)
            if d_plural == "deployments" and d_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                if not name_in or not _valid_name(name_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                    return
                if ns_in is not None and not _valid_name(ns_in):
                    self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                    return
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self._ok(_to_deployment(updated))
                return
            if d_plural == "deployments/scale" and d_name:
                obj = self.server.store.get("apps", "v1", "deployments", d_ns, d_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                desired = doc.get("spec", {}).get("replicas")
                if isinstance(desired, int) and desired >= 0:
                    spec = dict(obj.spec)
                    spec["replicas"] = desired
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "apps",
                        "v1",
                        "deployments",
                        d_ns,
                        d_name,
                        metadata=obj.metadata,
                        spec=spec,
                        status=_synthesize_deploy_status(spec, obj.status),
                    )
                    self._ok(_to_scale(updated))
                    return
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="spec.replicas must be >= 0")
                return
            if d_plural == "statefulsets" and d_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "statefulsets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "statefulsets"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self._ok(_to_statefulset(updated))
                return
            if d_plural == "daemonsets" and d_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or d_name
                ns_in = md.get("namespace") or d_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "daemonsets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "daemonsets"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._ok(_to_daemonset(updated))
                return
        if self.path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(self.path)
            if n_plural == "ingresses" and n_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or n_name
                ns_in = md.get("namespace") or n_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "networking.k8s.io",
                    "v1",
                    "ingresses",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "ingresses"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._ok(_to_ingress(updated))
                return
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions" and crd_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or crd_name
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apiextensions.k8s.io",
                    "v1",
                    "customresourcedefinitions",
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, "customresourcedefinitions"),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._register_crd(updated)
                self._ok(_to_crd(updated))
                return
        if self._handle_custom_resource_put(doc):
            return
        if self.path.startswith("/apis/batch/v1"):
            b_plural, b_ns, b_name = _batch_ns_name(self.path)
            if b_plural in {"jobs", "cronjobs"} and b_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or b_name
                ns_in = md.get("namespace") or b_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "batch",
                    "v1",
                    b_plural,
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, b_plural),
                    spec=doc.get("spec") or {},
                    status=doc.get("status") or {},
                )
                self._ok(_to_job(updated) if b_plural == "jobs" else _to_cronjob(updated))
                return
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if r_plural == plural and r_name:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or r_name
                    ns_in = md.get("namespace") or r_ns
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self._ok(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(updated))
                    return
            for plural, kind in (("clusterroles", "ClusterRole"), ("clusterrolebindings", "ClusterRoleBinding")):
                cr_plural, cr_name = _gv_cluster_name(self.path, "rbac.authorization.k8s.io", "v1", plural)
                if cr_plural == plural and cr_name:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or cr_name
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "rbac.authorization.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                    self._ok(_to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(updated))
                    return
        # policy/v1 PDB
        if self.path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(self.path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets" and p_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or p_name
                ns_in = md.get("namespace") or p_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "policy",
                    "v1",
                    "poddisruptionbudgets",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "poddisruptionbudgets"),
                    spec=_spec_payload("poddisruptionbudgets", doc),
                    status=doc.get("status") or {},
                )
                self._ok(_to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(updated))
                return
        # autoscaling/v2 HPA
        if self.path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(self.path, "autoscaling", "v2", "horizontalpodautoscalers")
            if h_plural == "horizontalpodautoscalers" and h_name:
                md = doc.get("metadata") or {}
                name_in = md.get("name") or h_name
                ns_in = md.get("namespace") or h_ns
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "autoscaling",
                    "v2",
                    "horizontalpodautoscalers",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "horizontalpodautoscalers"),
                    spec=_spec_payload("horizontalpodautoscalers", doc),
                    status=doc.get("status") or {},
                )
                self._ok(_to_hpa(updated, self.server.store))  # type: ignore[attr-defined]
                return
        self._not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authz():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        field_manager = q.get("fieldManager", ["kubectl"])[0] or "kubectl"
        force_flag = (q.get("force", ["false"])[0] or "").lower() in {"1", "true", "yes"}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        body = self._read_body()
        patch = _read_json(body)
        plural, ns, name = _ns_name(path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return
            base = _to_obj(obj)
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return
            md = merged.get("metadata") or {}
            patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
            if ctype.startswith("application/apply-patch") and _managed_conflict(obj.metadata, field_manager, patch_paths, force_flag):
                self._json_status(
                    HTTPStatus.CONFLICT,
                    reason="Conflict",
                    message="managedFields conflict on apply",
                )
                return
            if ctype.startswith("application/apply-patch"):
                md = _update_managed_fields(md, "v1", field_manager, "Apply", fields=patch_paths, force=force_flag)
            elif ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                md = _update_managed_fields(md, "v1", field_manager, "Update", fields=patch_paths)
            spec_or_data = merged.get("data") if plural in {"configmaps", "secrets"} else merged.get("spec")
            name_eff = md.get("name") or name
            ns_eff = None if plural == "namespaces" else (md.get("namespace") or ns)
            if not _valid_name(name_eff):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
                return
            if ns_eff is not None and not _valid_name(ns_eff):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.namespace (DNS-1123 label)")
                return
            updated = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_eff,
                name_eff,
                metadata=_normalize_metadata(md, name_eff, ns_eff, plural),
                spec=spec_or_data or {},
                status=merged.get("status") or {},
            )
            self._ok(_to_obj(updated))
            return
        orig_path = self.path
        self.path = path
        try:
            if self._handle_custom_resource_patch(ctype, patch):
                return
            if self._patch_extended_resources(ctype, patch, field_manager):
                return
        finally:
            self.path = orig_path
        self._not_found()

    def _patch_extended_resources(self, ctype: str, patch: Any, field_manager: str) -> bool:
        specs = [
            ("apps", "v1", "deployments", "Deployment"),
            ("apps", "v1", "statefulsets", "StatefulSet"),
            ("apps", "v1", "daemonsets", "DaemonSet"),
            ("batch", "v1", "jobs", "Job"),
            ("batch", "v1", "cronjobs", "CronJob"),
            ("rbac.authorization.k8s.io", "v1", "roles", "Role"),
            ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding"),
            ("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole"),
            ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding"),
            ("policy", "v1", "poddisruptionbudgets", "PodDisruptionBudget"),
            ("autoscaling", "v2", "horizontalpodautoscalers", "HorizontalPodAutoscaler"),
        ]
        transform_map = {
            ("apps", "v1", "deployments"): _to_deployment,
            ("apps", "v1", "statefulsets"): _to_statefulset,
            ("apps", "v1", "daemonsets"): _to_daemonset,
            ("batch", "v1", "jobs"): _to_job,
            ("batch", "v1", "cronjobs"): _to_cronjob,
            ("autoscaling", "v2", "horizontalpodautoscalers"): lambda o: _to_hpa(o, self.server.store),  # type: ignore[attr-defined]
        }
        for group, version, res, kind in specs:
            if not self.path.startswith(f"/apis/{group}/{version}"):
                continue
            if res.startswith("cluster"):
                plural, name = _gv_cluster_name(self.path, group, version, res)
                if plural != res or not name:
                    continue
                obj = self.server.store.get(group, version, res, None, name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return True
                base = transform_map.get((group, version, res), _to_generic(group, version, kind, res))(obj)  # type: ignore[arg-type]
                merged = self._apply_patch_merge(base, patch, ctype)
                if merged is None:
                    return True
                md = merged.get("metadata") or {}
                patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
                force_flag = (parse_qs(urlparse(self.path).query).get("force", ["false"])[0] or "").lower() in {"1", "true", "yes"}
                if ctype.startswith("application/apply-patch"):
                    if _managed_conflict(obj.metadata, field_manager, patch_paths, force_flag):
                        self._json_status(
                            HTTPStatus.CONFLICT,
                            reason="Conflict",
                            message="managedFields conflict on apply",
                        )
                        return True
                    md = _update_managed_fields(md, f"{group}/{version}", field_manager, "Apply", fields=patch_paths, force=force_flag)
                elif ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                    md = _update_managed_fields(md, f"{group}/{version}", field_manager, "Update", fields=patch_paths)
                # inject projections for workloads on any write
                if res in {"deployments", "statefulsets", "daemonsets", "jobs"}:
                    spec_body = merged.get("spec") or {}
                    merged["spec"] = _inject_sa_projection(spec_body)
                if res == "cronjobs":
                    cj_spec = merged.get("spec") or {}
                    jt = cj_spec.get("jobTemplate") or {}
                    jspec = _inject_sa_projection(jt.get("spec") or {})
                    jt["spec"] = jspec
                    cj_spec["jobTemplate"] = jt
                    merged["spec"] = cj_spec
                name_eff = md.get("name") or name
                updated = self.server.store.upsert(
                    group,
                    version,
                    res,
                    None,
                    name_eff,
                    metadata=_normalize_metadata(md, name_eff, None, res),
                    spec=_spec_payload(res, merged),
                    status=merged.get("status") or {},
                )  # type: ignore[attr-defined]
                self._ok(transform_map.get((group, version, res), _to_generic(group, version, kind, res))(updated))  # type: ignore[arg-type]
                return True
            plural, ns, name = _gv_ns_name(self.path, group, version, res)
            if plural != res or not name:
                continue
            obj = self.server.store.get(group, version, res, ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return True
            base = transform_map.get((group, version, res), _to_generic(group, version, kind, res))(obj)  # type: ignore[arg-type]
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return True
            md = merged.get("metadata") or {}
            patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
            if ctype.startswith("application/apply-patch"):
                force_flag = (parse_qs(urlparse(self.path).query).get("force", ["false"])[0] or "").lower() in {"1", "true", "yes"}
                if _managed_conflict(obj.metadata, field_manager, patch_paths, force_flag):
                    self._json_status(
                        HTTPStatus.CONFLICT,
                        reason="Conflict",
                        message="managedFields conflict on apply",
                    )
                    return True
                md = _update_managed_fields(md, f"{group}/{version}", field_manager, "Apply", fields=patch_paths, force=force_flag)
            elif ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                md = _update_managed_fields(md, f"{group}/{version}", field_manager, "Update", fields=patch_paths)
            if res in {"deployments", "statefulsets", "daemonsets", "jobs"}:
                spec_body = merged.get("spec") or {}
                merged["spec"] = _inject_sa_projection(spec_body)
            if res == "cronjobs":
                cj_spec = merged.get("spec") or {}
                jt = cj_spec.get("jobTemplate") or {}
                jspec = _inject_sa_projection(jt.get("spec") or {})
                jt["spec"] = jspec
                cj_spec["jobTemplate"] = jt
                merged["spec"] = cj_spec
            name_eff = md.get("name") or name
            ns_eff = md.get("namespace") or ns
            updated = self.server.store.upsert(
                group,
                version,
                res,
                ns_eff,
                name_eff,
                metadata=_normalize_metadata(md, name_eff, ns_eff, res),
                spec=_spec_payload(res, merged),
                status=merged.get("status") or {},
            )  # type: ignore[attr-defined]
            self._ok(transform_map.get((group, version, res), _to_generic(group, version, kind, res))(updated))  # type: ignore[arg-type]
            return True
        return False

    def _apply_patch_merge(
        self, base: dict[str, Any], patch: Any, ctype: str
    ) -> dict[str, Any] | None:
        if ctype == "application/json-patch+json":
            merged = _apply_json_patch(base, patch if isinstance(patch, list) else [])
            if merged is None:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="Invalid", message="invalid json patch")
            return merged
        if ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json", "application/apply-patch+yaml", "application/apply-patch+json"):
            return _merge_dict(base, patch)
        if ctype in ("application/json", ""):
            return patch if isinstance(patch, dict) else None
        self._json_status(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            reason="UnsupportedMediaType",
            message="only merge/strategic-merge, json, apply, or json-patch supported",
        )
        return None

    def _handle_custom_resource_get(self, path: str, query: dict[str, list[str]]) -> bool:
        parsed = _parse_custom_resource_path(path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        if namespaced:
            store_ns = namespace
        else:
            store_ns = None
            if namespace is not None:
                self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="resource is cluster-scoped; omit namespace")
                return True
        def transform(obj: dict[str, Any]) -> dict[str, Any]:
            return _render_custom_resource(obj, group, version, meta.get("kind", plural))
        if name is None:
            if query.get("watch", ["0"]) [0] in ("1", "true", "True"):
                self._stream_watch(group, version, plural, store_ns, query, transform)
                return True
            if namespaced and namespace is None:
                items = self.server.store.list_all(group, version, plural)  # type: ignore[attr-defined]
            else:
                items = self.server.store.list(group, version, plural, store_ns)  # type: ignore[attr-defined]
            self._ok(
                {
                    "kind": f"{meta.get('kind', plural)}List",
                    "apiVersion": f"{group}/{version}",
                    "items": [transform(i) for i in items],
                }
            )
            return True
        if namespaced and namespace is None:
            self._json_status(
                HTTPStatus.BAD_REQUEST,
                reason="BadRequest",
                message="namespaced resources require /namespaces/<name> in path",
            )
            return True
        obj = self.server.store.get(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not obj:
            self._not_found()
            return True
        self._ok(transform(obj))
        return True

    def _validate_app_custom_resource(self, doc: dict[str, Any]) -> str | None:
        # Validate App CRD payload against native schema to prevent incompatible objects.
        if (doc.get("apiVersion") or "").lower() not in {"ae.dev/v1alpha1"}:
            return "unsupported apiVersion for App (expected ae.dev/v1alpha1)"
        if (doc.get("kind") or "").lower() != "app":
            return "unsupported kind for ae.dev/v1alpha1 (expected App)"
        try:
            from ae.controller.spec import AppManifest  # imported lazily to avoid startup cost
        except Exception as exc:  # pragma: no cover - defensive import guard
            return f"unable to load App schema: {exc}"
        try:
            AppManifest.model_validate(doc)
        except Exception as exc:
            return f"App validation failed: {exc}"
        return None

    def _handle_custom_resource_post(self, doc: dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, path_name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        md = doc.get("metadata") or {}
        name_in = md.get("name") or path_name
        ns_in = md.get("namespace") or namespace
        if not name_in or not _valid_name(name_in):
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid or missing namespace")
                return True
        else:
            ns_in = None
        if group == "ae.dev" and plural == "apps":
            err = self._validate_app_custom_resource(doc)
            if err:
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message=err)
                return True
        created = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_in,
            name_in,
            metadata=_normalize_metadata(md, name_in, ns_in, plural),
            spec=doc.get("spec") or {},
            status=doc.get("status") or {},
        )
        self.send_response(HTTPStatus.CREATED)
        out = _json(_render_custom_resource(created, group, version, meta.get("kind", plural)))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
        return True

    def _handle_custom_resource_put(self, doc: dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, path_name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        md = doc.get("metadata") or {}
        name_in = md.get("name") or path_name
        ns_in = md.get("namespace") or namespace
        if not name_in or not _valid_name(name_in):
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid metadata.name (DNS-1123 label)")
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="invalid or missing namespace")
                return True
        else:
            ns_in = None
        if group == "ae.dev" and plural == "apps":
            err = self._validate_app_custom_resource(doc)
            if err:
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message=err)
                return True
        updated = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_in,
            name_in,
            metadata=_normalize_metadata(md, name_in, ns_in, plural),
            spec=doc.get("spec") or {},
            status=doc.get("status") or {},
        )
        self._ok(_render_custom_resource(updated, group, version, meta.get("kind", plural)))
        return True

    def _handle_custom_resource_patch(self, ctype: str, patch: dict[str, Any]) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        store_ns = namespace if namespaced else None
        obj = self.server.store.get(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not obj:
            self._not_found()
            return True
        base = _render_custom_resource(obj, group, version, meta.get("kind", plural))
        merged = self._apply_patch_merge(base, patch, ctype)
        if merged is None:
            return True
        md = merged.get("metadata") or {}
        name_eff = md.get("name") or name
        ns_eff = md.get("namespace") or namespace
        if namespaced and not ns_eff:
            self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="missing namespace")
            return True
        if not namespaced:
            ns_eff = None
        if group == "ae.dev" and plural == "apps":
            err = self._validate_app_custom_resource(merged)
            if err:
                self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message=err)
                return True
        updated = self.server.store.upsert(  # type: ignore[attr-defined]
            group,
            version,
            plural,
            ns_eff,
            name_eff,
            metadata=_normalize_metadata(md, name_eff, ns_eff, plural),
            spec=merged.get("spec") or {},
            status=merged.get("status") or {},
        )
        self._ok(_render_custom_resource(updated, group, version, meta.get("kind", plural)))
        return True

    def _handle_custom_resource_delete(self) -> bool:
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        if namespaced and namespace is None:
            self._json_status(HTTPStatus.BAD_REQUEST, reason="BadRequest", message="namespaced resource delete requires namespace")
            return True
        store_ns = namespace if namespaced else None
        ok = self.server.store.delete(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not ok:
            self._not_found()
            return True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
        return True

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authz():
            return
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            ok = self.server.store.delete("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
            if not ok:
                self._not_found()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions" and crd_name:
                ok = self.server.store.delete("apiextensions.k8s.io", "v1", "customresourcedefinitions", None, crd_name)  # type: ignore[attr-defined]
                if not ok:
                    self._not_found()
                    return
                self._unregister_crd(crd_name)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
        if self._handle_custom_resource_delete():
            return
        self._not_found()


def _kind(plural: str) -> str:
    return {
        "namespaces": "Namespace",
        "configmaps": "ConfigMap",
        "secrets": "Secret",
        "serviceaccounts": "ServiceAccount",
        "services": "Service",
    }[plural]


def _api_version(group: str, version: str) -> str:
    return f"{group}/{version}" if group else version


def _to_obj(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    return {
        "apiVersion": _api_version(o.group, o.version),
        "kind": _kind(o.resource),
        "metadata": meta,
        **(
            {"data": o.spec}
            if o.resource in {"configmaps", "secrets"}
            else ({} if not o.spec else {"spec": o.spec})
        ),
        **({} if not o.status else {"status": o.status}),
    }


def _to_deployment(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    # attach/ensure generation
    gen_val = meta.get("generation")
    try:
        gen = int(gen_val) if gen_val is not None else 1
    except Exception:
        gen = 1
    meta["generation"] = gen
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": _synthesize_deploy_status(o.spec, o.status),
    }


def _synthesize_deploy_status(spec: dict[str, Any], base_status: dict[str, Any]) -> dict[str, Any]:
    replicas = int(spec.get("replicas", 1))
    available = replicas
    ready = replicas
    updated = replicas
    status = dict(base_status)
    status.update(
        {
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "conditions": [
                {
                    "type": "Available",
                    "status": "True" if available >= replicas else "False",
                    "reason": "MinimumReplicasAvailable",
                },
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
            ],
        }
    )
    return status


def _to_statefulset(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    gen = meta.get("generation")
    try:
        meta["generation"] = int(gen) if gen is not None else 1
    except Exception:
        meta["generation"] = 1
    status = _synthesize_deploy_status(o.spec, o.status)
    # StatefulSet fields differ slightly
    status.setdefault("readyReplicas", status.get("availableReplicas", 0))
    status.setdefault("currentReplicas", status.get("updatedReplicas", status.get("replicas", 0)))
    status.setdefault("currentRevision", meta.get("generation"))
    status.setdefault("updateRevision", meta.get("generation"))
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": status,
    }


def _to_daemonset(o: K8sObject, *, desired: int | None = None) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    gen = meta.get("generation")
    try:
        meta["generation"] = int(gen) if gen is not None else 1
    except Exception:
        meta["generation"] = 1
    replicas = desired if desired is not None else int(o.spec.get("replicas", 1))
    st = dict(o.status or {})
    st.setdefault("desiredNumberScheduled", replicas)
    st.setdefault("currentNumberScheduled", replicas)
    st.setdefault("numberReady", replicas)
    st.setdefault("numberAvailable", replicas)
    st.setdefault("updatedNumberScheduled", replicas)
    st.setdefault(
        "conditions",
        [
            {"type": "Ready", "status": "True"},
            {"type": "Available", "status": "True"},
        ],
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": st,
    }


def _to_job(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    status = dict(o.status or {})
    completions = int(o.spec.get("completions", o.spec.get("parallelism", 1) or 1))
    status.setdefault("active", 0)
    status.setdefault("succeeded", status.get("succeeded", 0))
    status.setdefault("failed", status.get("failed", 0))
    status.setdefault("conditions", [])
    if status.get("succeeded", 0) >= completions:
        status["conditions"] = [{"type": "Complete", "status": "True"}]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": status,
    }


def _to_cronjob(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    status = dict(o.status or {})
    status.setdefault("active", [])
    status.setdefault("lastScheduleTime", status.get("lastScheduleTime"))
    status.setdefault("lastSuccessfulTime", status.get("lastSuccessfulTime"))
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": status,
    }


def _to_scale(o: K8sObject) -> dict[str, Any]:
    meta = {"name": o.name}
    if o.namespace:
        meta["namespace"] = o.namespace
    replicas = int(o.spec.get("replicas", 1))
    return {
        "apiVersion": "autoscaling/v1",
        "kind": "Scale",
        "metadata": meta,
        "spec": {"replicas": replicas},
        "status": {"replicas": replicas, "selector": ""},
    }


def _to_hpa(o: K8sObject, store: ObjectStore) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    status = dict(o.status or {})
    # Best-effort scaleTargetRef resolution for status
    spec = o.spec or {}
    target = spec.get("scaleTargetRef", {}) if isinstance(spec, dict) else {}
    target_name = target.get("name")
    target_kind = (target.get("kind") or "").lower()
    current_replicas = status.get("currentReplicas")
    desired = status.get("desiredReplicas")
    try:
        if target_kind == "deployment":
            obj = store.get("apps", "v1", "deployments", o.namespace, target_name)  # type: ignore[arg-type]
            if obj:
                current_replicas = current_replicas or int(obj.spec.get("replicas", 1))
                desired = desired or int(obj.spec.get("replicas", 1))
        elif target_kind == "statefulset":
            obj = store.get("apps", "v1", "statefulsets", o.namespace, target_name)  # type: ignore[arg-type]
            if obj:
                current_replicas = current_replicas or int(obj.spec.get("replicas", 1))
                desired = desired or int(obj.spec.get("replicas", 1))
    except Exception:
        pass
    status.setdefault("currentReplicas", current_replicas or 0)
    status.setdefault("desiredReplicas", desired or status.get("currentReplicas", 0))
    status.setdefault("conditions", status.get("conditions", []))
    status.setdefault("currentMetrics", status.get("currentMetrics", []))
    # simple backoff to avoid flapping: mark 'AbleToScale' condition true if we have any target
    if not any(c.get("type") == "AbleToScale" for c in status["conditions"]):
        status["conditions"].append({"type": "AbleToScale", "status": "True"})
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": meta,
        "spec": spec,
        "status": status,
    }


def _to_event(namespace: str, obj_name: str, ev: AppEvent) -> dict[str, Any]:  # type: ignore[name-defined]
    ts = ev.created_at.isoformat()
    involved = {
        "kind": "Deployment",
        "name": obj_name,
        "namespace": namespace,
        "uid": f"{namespace}-{obj_name}",
    }
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {
            "name": f"{obj_name}.{int(ev.created_at.timestamp())}",
            "namespace": namespace,
            "creationTimestamp": ts,
        },
        "involvedObject": involved,
        "reason": ev.event_type,
        "message": ev.message,
        "type": ev.event_type.upper() if ev.event_type else "Normal",
        "eventTime": ts,
        "firstTimestamp": ts,
        "lastTimestamp": ts,
    }


def _ingress_vip(state: SQLiteStateStore, ing: K8sObject) -> str | None:
    """Best-effort: use first backend service to derive VIP/clusterIP."""
    spec = ing.spec or {}
    svc_name = None
    try:
        if spec.get("defaultBackend", {}).get("service"):
            svc_name = spec["defaultBackend"]["service"].get("name")
        if not svc_name:
            for rule in spec.get("rules", []):
                paths = (rule.get("http") or {}).get("paths") or []
                if paths:
                    backend = paths[0].get("backend", {})
                    svc = backend.get("service", {})
                    svc_name = svc.get("name")
                    if svc_name:
                        break
    except Exception:
        svc_name = None
    if not svc_name:
        return None
    try:
        svc_obj = state.get("", "v1", "services", ing.namespace, svc_name)  # type: ignore[attr-defined]
    except Exception:
        svc_obj = None
    if svc_obj:
        prov_ip = _provider_cluster_ip(state, svc_obj)  # type: ignore[arg-type]
        return prov_ip or svc_obj.spec.get("clusterIP") or None
    return None


def _to_ingress(o: K8sObject, state: SQLiteStateStore | None = None) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    out = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta,
        "spec": dict(o.spec),
    }
    status = dict(o.status or {})
    if state is not None:
        vip = _ingress_vip(state, o)
        if vip:
            lb = status.get("loadBalancer") or {}
            ingress = lb.get("ingress") or []
            if not any(entry.get("ip") == vip for entry in ingress):
                ingress.append({"ip": vip})
            lb["ingress"] = ingress
            status["loadBalancer"] = lb
    if status:
        out["status"] = status
    return out


def _list_with_rv(
    items: list[K8sObject],
    transform,
    *,
    kind: str,
    api_version: str,
    limit: int | None = None,
    continue_token: str | None = None,
) -> dict[str, Any]:
    selected = items
    cont_token: str | None = None
    if continue_token:
        for idx, it in enumerate(items):
            if getattr(it, "name", None) == continue_token:
                selected = items[idx + 1 :]
                break
    if isinstance(limit, int) and limit > 0 and len(items) > limit:
        selected = items[:limit]
        cont_token = items[limit].name if len(items) > limit else None
    rv = max((i.resource_version for i in selected), default=0)
    meta: dict[str, Any] = {"resourceVersion": str(rv)}
    if cont_token:
        meta["continue"] = cont_token
    return {
        "kind": f"{kind}List",
        "apiVersion": api_version,
        "metadata": meta,
        "items": [transform(i) for i in selected],
    }


def _to_crd(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    body = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": meta,
        "spec": dict(o.spec),
    }
    if o.status:
        body["status"] = o.status
    return body


def _apps_ns_name(path: str) -> tuple[str, str | None, str | None]:
    # Strip query parameters so regexes match kubectl requests that include
    # ?fieldManager=... / ?fieldValidation=...
    path = path.split("?", 1)[0]
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments/([^/]+)/(status|scale)$", path)
    if m:
        return (f"deployments/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/statefulsets(?:/([^/]+))?$", path)
    if m:
        return ("statefulsets", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/statefulsets/([^/]+)/(status|scale)$", path)
    if m:
        return (f"statefulsets/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/daemonsets(?:/([^/]+))?$", path)
    if m:
        return ("daemonsets", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/daemonsets/([^/]+)/(status)$", path)
    if m:
        return (f"daemonsets/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", None, m.group(1))
    m = re.match(r"^/apis/apps/v1/statefulsets(?:/([^/]+))?$", path)
    if m:
        return ("statefulsets", None, m.group(1))
    m = re.match(r"^/apis/apps/v1/daemonsets(?:/([^/]+))?$", path)
    if m:
        return ("daemonsets", None, m.group(1))
    return ("", None, None)


def _net_ns_name(path: str) -> tuple[str, str | None, str | None]:
    m = re.match(r"^/apis/networking.k8s.io/v1/namespaces/([^/]+)/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", m.group(1), m.group(2))
    m = re.match(r"^/apis/networking.k8s.io/v1/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", None, m.group(1))
    return ("", None, None)


def _gv_ns_name(path: str, group: str, version: str, plural: str) -> tuple[str, str | None, str | None]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/namespaces/([^/]+)/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1), m.group(2))
    pattern_all = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern_all, path)
    if m:
        return (plural, None, m.group(1))
    return ("", None, None)


def _batch_ns_name(path: str) -> tuple[str, str | None, str | None]:
    m = re.match(r"^/apis/batch/v1/namespaces/([^/]+)/jobs(?:/([^/]+))?$", path)
    if m:
        return ("jobs", m.group(1), m.group(2))
    m = re.match(r"^/apis/batch/v1/namespaces/([^/]+)/jobs/([^/]+)/(status)$", path)
    if m:
        return (f"jobs/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/batch/v1/namespaces/([^/]+)/cronjobs(?:/([^/]+))?$", path)
    if m:
        return ("cronjobs", m.group(1), m.group(2))
    m = re.match(r"^/apis/batch/v1/namespaces/([^/]+)/cronjobs/([^/]+)/(status)$", path)
    if m:
        return (f"cronjobs/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/batch/v1/jobs(?:/([^/]+))?$", path)
    if m:
        return ("jobs", None, m.group(1))
    m = re.match(r"^/apis/batch/v1/cronjobs(?:/([^/]+))?$", path)
    if m:
        return ("cronjobs", None, m.group(1))
    return ("", None, None)


def _gv_cluster_name(path: str, group: str, version: str, plural: str) -> tuple[str, str | None]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1))
    return ("", None)


def _to_generic(group: str, version: str, kind: str, resource: str):
    def convert(o: K8sObject) -> dict[str, Any]:
        meta = dict(o.metadata)
        meta.setdefault("name", o.name)
        if o.namespace:
            meta.setdefault("namespace", o.namespace)
        meta.setdefault("resourceVersion", str(o.resource_version))
        body: dict[str, Any] = {
            "apiVersion": _api_version(group, version),
            "kind": kind,
            "metadata": meta,
        }
        data = dict(o.spec)
        if resource in {"roles", "clusterroles"}:
            body["rules"] = data.get("rules", [])
        elif resource in {"rolebindings", "clusterrolebindings"}:
            if data.get("roleRef"):
                body["roleRef"] = data["roleRef"]
            if data.get("subjects") is not None:
                body["subjects"] = data.get("subjects", [])
        elif resource == "poddisruptionbudgets":
            body["spec"] = data.get("spec", data)
            if o.status:
                body["status"] = o.status
        elif resource == "horizontalpodautoscalers":
            body["spec"] = data.get("spec", data)
            if o.status:
                body["status"] = o.status
        else:
            if data:
                body["spec"] = data
            if o.status:
                body["status"] = o.status
        return body

    return convert


def _spec_payload(resource: str, merged: dict[str, Any]) -> dict[str, Any]:
    if resource in {"roles", "clusterroles"}:
        return {"rules": merged.get("rules", [])}
    if resource in {"rolebindings", "clusterrolebindings"}:
        return {
            "subjects": merged.get("subjects", []),
            "roleRef": merged.get("roleRef"),
        }
    if resource == "poddisruptionbudgets":
        return {"spec": merged.get("spec", merged.get("body", {}))}
    if resource == "horizontalpodautoscalers":
        return {"spec": merged.get("spec", merged.get("body", {}))}
    return merged.get("spec") or {}


def _render_custom_resource(o: K8sObject, group: str, version: str, kind: str) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    body: dict[str, Any] = {
        "apiVersion": f"{group}/{version}",
        "kind": kind,
        "metadata": meta,
    }
    if o.spec:
        body["spec"] = o.spec
    if o.status:
        body["status"] = o.status
    return body


def _parse_custom_resource_path(path: str) -> tuple[str, str, str | None, str, str | None] | None:
    m = re.match(
        r"^/apis/([^/]+)/([^/]+)(?:/namespaces/([^/]+))?/([^/]+)(?:/([^/]+))?$",
        path,
    )
    if not m:
        return None
    group, version, namespace, plural, name = m.groups()
    if group in RESERVED_GROUPS and plural in {
        "deployments",
        "deployments/status",
        "deployments/scale",
        "ingresses",
        "customresourcedefinitions",
    }:
        return None
    return (group, version, namespace, plural, name)


def _normalize_metadata(md: dict[str, Any], name: str, ns: str | None, plural: str) -> dict[str, Any]:
    out = dict(md)
    out["name"] = name
    if ns and plural != "namespaces":
        out["namespace"] = ns
    return out


def _merge_list(base: list, patch: list) -> list:
    """Strategic-ish merge for lists of maps keyed by a stable identifier."""

    if not isinstance(base, list) or not isinstance(patch, list):
        return patch
    # If patch contains scalars or empty list, replace outright
    if patch and not isinstance(patch[0], dict):
        return patch
    if base and not isinstance(base[0], dict):
        return patch

    base_idx: dict[Any, dict[str, Any]] = {}
    merged: list = []

    # Seed with base preserving order
    for item in base:
        if isinstance(item, dict):
            key = _list_item_key(item)
            if key is not None:
                base_idx[key] = dict(item)
                merged.append(base_idx[key])
                continue
        merged.append(item)

    for item in patch:
        if isinstance(item, dict):
            key = _list_item_key(item)
            if key is not None and key in base_idx:
                # merge dicts with same key
                base_idx[key].update(_merge_dict(base_idx[key], item))  # type: ignore[arg-type]
                continue
        merged.append(item)
    return merged


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)  # type: ignore[arg-type]
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = _merge_list(out[k], v)
        else:
            out[k] = v
    return out


_DNS1123_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


def _valid_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if not name or len(name) > 253:
        return False
    return _DNS1123_RE.match(name) is not None


def _service_target(svc: K8sObject) -> str | None:
    spec = svc.spec or {}
    selector = spec.get("selector") or {}
    if not selector:
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
    return (
        selector.get("app")
        or selector.get("app.kubernetes.io/name")
        or svc.metadata.get("labels", {}).get("app")
        or svc.metadata.get("annotations", {}).get("apishim.k1s.dev/app")
        or svc.metadata.get("name")
    )


def _service_app_name(svc: K8sObject) -> str | None:
    tgt = _service_target(svc)
    if not tgt:
        return None
    return f"{svc.namespace}--{tgt}" if svc.namespace else tgt


def _provider_cluster_ip(state: SQLiteStateStore, svc: K8sObject) -> str | None:
    """Fetch cluster IP allocated by the network provider (if recorded in controller state)."""
    app_name = _service_app_name(svc)
    if not app_name:
        return None
    try:
        rec = state.get_service(app_name)  # type: ignore[attr-defined]
        return rec.cluster_ip if rec else None
    except Exception:
        return None


def _provider_ports(state: SQLiteStateStore, svc: K8sObject) -> dict:
    """Fetch provider-recorded port info (including nodePort) for a service, keyed by port name/number."""
    app_name = _service_app_name(svc)
    if not app_name:
        return {}
    try:
        rec = state.get_service(app_name)  # type: ignore[attr-defined]
    except Exception:
        rec = None
    if not rec or not rec.ports:
        return {}
    return rec.ports


def _provider_vip(state: SQLiteStateStore, svc: K8sObject) -> str | None:
    """Return overlay/proxy VIP if recorded by the network provider."""
    app_name = _service_app_name(svc)
    if not app_name:
        return None
    try:
        rec = state.get_service(app_name)  # type: ignore[attr-defined]
    except Exception:
        rec = None
    # Prefer cluster_ip recorded in provider; that is our VIP
    if rec and getattr(rec, "cluster_ip", None):
        return rec.cluster_ip
    return None


def _merge_provider_service(state: SQLiteStateStore, doc: dict[str, Any], svc_obj: K8sObject) -> dict[str, Any]:
    """Augment service spec/status with provider allocations (clusterIP/nodePort)."""
    spec = doc.get("spec") or {}
    status = doc.get("status") or {}
    prov_ip = _provider_cluster_ip(state, svc_obj)
    vip = _provider_vip(state, svc_obj) or prov_ip
    if prov_ip:
        if spec.get("clusterIP") in {None, "", "None"}:
            spec["clusterIP"] = prov_ip
        status = _service_lb_status(spec, status, vip)
    # fill nodePorts from provider record if missing
    prov_ports = _provider_ports(state, svc_obj)
    if prov_ports and spec.get("ports"):
        new_ports = []
        for p in spec.get("ports", []):
            p = dict(p)
            key = p.get("name") or str(p.get("port"))
            rec = prov_ports.get(key) or prov_ports.get(str(key))
            if rec and rec.get("nodePort") is not None:
                if p.get("nodePort") is None or p.get("nodePort") != rec["nodePort"]:
                    p["nodePort"] = rec["nodePort"]
            new_ports.append(p)
        spec["ports"] = new_ports
    doc["spec"] = spec
    doc["status"] = _service_lb_status(spec, status, prov_ip)
    return doc


def _node_zone_for_ip(state: SQLiteStateStore, ip: str) -> tuple[str | None, str | None]:
    """Best-effort mapping from pod IP to node name/zone using node podCIDR labels."""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except Exception:
        return (None, None)
    try:
        nodes = state.list_nodes()  # type: ignore[attr-defined]
    except Exception:
        nodes = []
    for rec, _st in nodes:
        try:
            if rec.pod_cidr and ip_obj in ipaddress.ip_network(rec.pod_cidr):
                zone = (rec.labels or {}).get("topology.kubernetes.io/zone") or (rec.labels or {}).get("failure-domain.beta.kubernetes.io/zone")
                name = rec.name or rec.node_id
                return (name, zone)
        except Exception:
            continue
    return (None, None)


def _alloc_cluster_ip(ns: str | None, name: str, existing: set[str]) -> str:
    """Deterministically allocate a ClusterIP in 10.96.0.0/16 avoiding collisions."""
    base_int = 0x0A600000  # 10.96.0.0
    mask = 0xFFFF
    h = hash(f"{ns or ''}/{name}") & mask
    for offset in range(mask):
        ip_int = base_int + ((h + offset) & mask)
        # skip network/broadcast-ish low addrs
        if ip_int & 0xFF in {0, 255}:
            continue
        ip = ".".join(str((ip_int >> (8 * i)) & 0xFF) for i in reversed(range(4)))
        if ip not in existing:
            return ip
    # fallback, shouldn't happen
    return "10.96.255.254"


def _alloc_nodeport(existing: set[int], seed: str) -> int:
    NP_MIN, NP_MAX = 30000, 32767
    span = NP_MAX - NP_MIN + 1
    h = abs(hash(seed)) % span
    for offset in range(span):
        cand = NP_MIN + ((h + offset) % span)
        if cand not in existing:
            return cand
    return NP_MIN


def _service_lb_status(spec: dict[str, Any], status: dict[str, Any], provider_ip: str | None = None) -> dict[str, Any]:
    """Ensure loadBalancer status is present for LB/NodePort services."""
    svc_type = (spec.get("type") or "ClusterIP") or "ClusterIP"
    status = dict(status or {})
    if svc_type in {"LoadBalancer", "NodePort"}:
        lb = status.get("loadBalancer") or {}
        ingress = lb.get("ingress") or []
        # Preserve existing ingress if set
        if not ingress:
            # provider VIP first
            if provider_ip:
                ingress.append({"ip": provider_ip})
            # loadBalancerIP hint
            lb_ip = spec.get("loadBalancerIP")
            if lb_ip:
                ingress.append({"ip": lb_ip})
            # externalIPs as ingress hints
            for ext in spec.get("externalIPs") or []:
                if isinstance(ext, str):
                    ingress.append({"ip": ext})
            # fall back to clusterIP so clients see a reachable address
            if not ingress:
                ip = spec.get("clusterIP")
                if ip and ip not in {"None", None}:
                    ingress = [{"ip": ip}]
        # Add externalIPs as ingress hints
        else:
            for ext in spec.get("externalIPs") or []:
                if isinstance(ext, str):
                    ingress.append({"ip": ext})
        lb["ingress"] = ingress
        status["loadBalancer"] = lb
    return status


def _pick_endpoint_ip(endpoints: list[ServiceEndpoint], key: str | None = None) -> str | None:
    """Choose a ready endpoint IP if available; fall back to first.

    Selection is keyed (e.g., port list) for stable-ish distribution.
    """
    ready = [ep.ip for ep in endpoints if ep.ready]
    candidates = ready or [ep.ip for ep in endpoints]
    if not candidates:
        return None
    salt = key or ""
    try:
        idx = abs(hash(salt)) % len(candidates)
    except Exception:
        idx = int(time.time() * 1000) % len(candidates)
    return candidates[idx]


def _endpoints_for_service(state: SQLiteStateStore, svc: K8sObject) -> dict[str, Any] | None:
    app_name = _service_app_name(svc)
    if not app_name:
        return None
    endpoints = state.list_service_endpoints(app_name)
    ports_spec = []
    for p in svc.spec.get("ports", []):
        ports_spec.append(
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "protocol": p.get("protocol", "TCP"),
            }
        )
    ready_addrs = []
    not_ready = []
    for ep in endpoints:
        entry = {"ip": ep.ip}
        if ep.ready:
            ready_addrs.append(entry)
        else:
            not_ready.append(entry)
    meta = {
        "name": svc.name,
        "namespace": svc.namespace,
        "resourceVersion": str(svc.resource_version),
    }
    body = {
        "apiVersion": "v1",
        "kind": "Endpoints",
        "metadata": meta,
        "subsets": [
            {
                "addresses": ready_addrs or [],
                "notReadyAddresses": not_ready or [],
                "ports": ports_spec,
            }
        ],
    }
    return body


def _endpointslice_for_service(state: SQLiteStateStore, svc: K8sObject) -> dict[str, Any] | None:
    """Project a single EndpointSlice per Service using controller endpoints."""
    target = _service_target(svc)
    if not target:
        return None
    app_name = f"{svc.namespace}--{target}" if svc.namespace else target
    endpoints = state.list_service_endpoints(app_name)
    if not endpoints:
        return None
    ready_eps = []
    not_ready_eps = []
    for ep in endpoints:
        node_name, zone = _node_zone_for_ip(state, ep.ip)
        entry = {
            "addresses": [ep.ip],
            "conditions": {"ready": bool(ep.ready)},
            "targetRef": {
                "kind": "Pod",
                "name": ep.pod_name or ep.app_name,
                "namespace": svc.namespace,
            },
        }
        if node_name:
            entry["nodeName"] = node_name
        if zone:
            entry["zone"] = zone
            entry["hints"] = {"forZones": [{"name": zone}]}
        if ep.ready:
            ready_eps.append(entry)
        else:
            not_ready_eps.append(entry)
    ports_spec = []
    for p in svc.spec.get("ports", []):
        ports_spec.append(
            {
                "name": p.get("name"),
                "port": p.get("port"),
                "protocol": p.get("protocol", "TCP"),
                "appProtocol": p.get("appProtocol"),
            }
        )
    name_suffix = svc.metadata.get("resourceVersion") or svc.resource_version
    slice_name = f"{svc.name}-{name_suffix}"
    meta = {
        "name": slice_name,
        "namespace": svc.namespace,
        "labels": {
            "kubernetes.io/service-name": svc.name,
        },
        "resourceVersion": str(svc.resource_version),
    }
    body = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": meta,
        "addressType": "IPv4",
        "ports": ports_spec,
        "endpoints": ready_eps + not_ready_eps,
    }
    return body


def _node_obj(record, status, rv: int) -> dict[str, Any]:
    meta = {
        "name": record.name or record.node_id,
        "resourceVersion": str(rv),
        "labels": record.labels or {},
    }
    conditions: list[dict[str, Any]] = []
    if status:
        conditions.append(
            {
                "type": "Ready",
                "status": "True" if status.status == "ready" else "False",
                "lastHeartbeatTime": status.seen_at.isoformat(),
                "reason": "AgentHeartbeat",
            }
        )
    else:
        conditions.append({"type": "Ready", "status": "Unknown"})
    node_status = {"conditions": conditions}
    return {"apiVersion": "v1", "kind": "Node", "metadata": meta, "status": node_status}


def _runtime_from_env() -> RuntimeAdapter:
    backend = (os.getenv("AE_APISHIM_RUNTIME") or os.getenv("AE_RUNTIME_BACKEND") or "stub").lower()
    if backend in {"stub", "test"}:
        return StubRuntime()
    if backend in {"podman", "oci"}:
        try:
            return PodmanRuntime()
        except Exception:
            return DockerRuntime()
    if backend == "remote":
        return RemoteRuntime()
    return DockerRuntime()


def _pod_obj(container: dict, rv: int, node_name: str | None) -> dict[str, Any]:
    labels = container.get("labels", {}) or {}
    replica_id = labels.get("ae.replica_id") or container.get("name") or "replica"
    ns = "default"
    meta = {
        "name": replica_id,
        "namespace": ns,
        "labels": labels,
        "resourceVersion": str(rv),
    }
    running = bool(container.get("running", False))
    restart_count = int(container.get("restart_count", 0) or 0)
    started_at = container.get("started_at") or None
    state_obj: dict[str, Any]
    if running:
        state_obj = {"running": {"startedAt": started_at}}
        phase = "Running"
        ready = True
    else:
        state_obj = {"waiting": {"reason": "ContainerCreating"}}
        phase = "Pending"
        ready = False
    # capture last state if present
    last_state = {}
    if container.get("last_exit_code") is not None:
        last_state = {
            "terminated": {
                "exitCode": int(container.get("last_exit_code", 0)),
                "reason": container.get("last_reason") or "Completed",
                "finishedAt": container.get("last_finished_at"),
            }
        }
    status = {
        "phase": "Running",
        "podIP": container.get("pod_ip"),
        "hostIP": container.get("host_ip"),
        "containerStatuses": [
            {
                "name": labels.get("ae.container", "main"),
                "ready": ready,
                "restartCount": restart_count,
                "state": state_obj,
                "lastState": last_state,
            }
        ],
        "conditions": [
            {"type": "PodScheduled", "status": "True"},
            {"type": "Ready", "status": "True" if ready else "False"},
            {"type": "ContainersReady", "status": "True" if ready else "False"},
        ],
    }
    if node_name:
        meta["nodeName"] = node_name
    status["phase"] = phase
    sa_name = labels.get("ae.service_account") or "default"
    spec: dict[str, Any] = _inject_sa_projection(
        {
            "nodeName": node_name,
            "serviceAccountName": sa_name,
            "containers": [
                {
                    "name": labels.get("ae.container", "main"),
                }
            ],
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": meta,
        "spec": spec,
        "status": status,
    }


class ShimServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], token: str | None, allow_anonymous: bool = False) -> None:
        super().__init__(server_address, ShimHandler)
        dsn = os.getenv("AE_APISHIM_DSN")
        db_path = Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))
        self.store = ObjectStore(db_path=db_path, dsn=dsn)
        ShimHandler.admin_token = token or os.getenv("AE_APISHIM_TOKEN")
        ShimHandler.read_token = os.getenv("AE_APISHIM_READ_TOKEN")
        ShimHandler.allow_anonymous = allow_anonymous
        state_dsn = os.getenv("AE_STATE_DSN")
        db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
        self.state = SQLiteStateStore(db_path if not state_dsn else None, dsn=state_dsn)
        ShimHandler.state = self.state  # type: ignore[assignment]
        self.runtime = _runtime_from_env()
        self._bootstrap_crds()
        # Start adapter worker to reconcile apps/v1 Deployments into k1s
        try:
            self._adapter = build_adapter(self.store, runtime=self.runtime)
            self._adapter.start()
        except Exception:
            self._adapter = None

    def _bootstrap_crds(self) -> None:
        try:
            objs = self.store.list_all("apiextensions.k8s.io", "v1", "customresourcedefinitions")
        except Exception:
            objs = []
        for obj in objs:
            ShimHandler._register_crd(obj)


def run_server(host: str = "127.0.0.1", port: int = 8445, token: str | None = None, tls: bool = False, allow_anonymous: bool = False) -> None:
    if os.getenv("AE_APISHIM_ENABLE") != "1":
        raise RuntimeError("apishim disabled: set AE_APISHIM_ENABLE=1 to start the shim server")
    allow_anonymous = allow_anonymous or os.getenv("AE_APISHIM_ALLOW_ANON", "0") == "1"
    tok = token or os.getenv("AE_APISHIM_TOKEN")
    if not tok and not allow_anonymous:
        raise RuntimeError("AE_APISHIM_TOKEN must be set (or --token) to start the shim server (or set AE_APISHIM_ALLOW_ANON=1 for dev)")
    httpd = ShimServer((host, port), tok, allow_anonymous=allow_anonymous)
    if tls:
        # Dev TLS: requires user-provided cert/key via env or skip.
        cert_file = os.getenv("AE_APISHIM_TLS_CERT")
        key_file = os.getenv("AE_APISHIM_TLS_KEY")
        if not (cert_file and key_file):
            raise RuntimeError("TLS requested but AE_APISHIM_TLS_CERT/KEY not set")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        client_ca = os.getenv("AE_APISHIM_TLS_CLIENT_CA")
        if client_ca:
            ctx.load_verify_locations(cafile=client_ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ShimHandler.client_cert_required = True
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
