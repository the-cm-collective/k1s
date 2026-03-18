# ruff: noqa: E501,S105,S110,S112,SIM102,SIM105,SIM108,SIM114,SIM118,SIM300
"""HTTP server implementing a Kubernetes-compatible API for the shim."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import json as _jsonlib
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ae.controller.state import (
    AppEvent,
    RegistryConflictError,
    ServiceEndpoint,
    SQLiteStateStore,
    state_store_from_env,
)
from ae.runtime import (
    CRIRuntime,
    DockerRuntime,
    PodmanRuntime,
    RemoteRuntime,
    RuntimeAdapter,
    StubRuntime,
)

from .adapter import build_adapter
from .ha_store import (
    AuthorityMutationError,
    MultiplexApishimStore,
    is_controller_owned_storage_authority_resource,
)
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

LOGGER = logging.getLogger(__name__)
SPDY_DEBUG = str(os.getenv("AE_APISHIM_SPDY_DEBUG", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PF_DEBUG = str(os.getenv("AE_APISHIM_PF_DEBUG", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _json(d: dict[str, Any]) -> bytes:
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def _spdy_debug_line(message: str) -> None:
    path = os.getenv("AE_APISHIM_SPDY_LOG", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        # Best-effort logging; never break request handling.
        return


def _exec_status_obj(exit_code: int) -> dict[str, Any]:
    details: dict[str, Any] = {"exitCode": exit_code}
    status = "Success"
    reason = ""
    message = ""
    code = 0
    if exit_code != 0:
        status = "Failure"
        reason = "NonZeroExitCode"
        message = f"command terminated with non-zero exit code: {exit_code}"
        details["causes"] = [{"reason": "ExitCode", "message": str(exit_code)}]
        code = 1
    return {
        "metadata": {},
        "status": status,
        "message": message,
        "reason": reason,
        "code": code,
        "details": details,
    }


def _read_json(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except Exception:
            return {}
        try:
            return yaml.safe_load(text) or {}
        except Exception:
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
    from ae.controller.spec import app_key

    return app_key(name, ns)


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


def _split_selector_terms(selector: str) -> list[str]:
    terms: list[str] = []
    if not selector:
        return terms
    buf: list[str] = []
    depth = 0
    for ch in selector:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        if ch == "," and depth == 0:
            term = "".join(buf).strip()
            if term:
                terms.append(term)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        terms.append(tail)
    return terms


def _split_selector_values(raw: str) -> list[str]:
    return [val.strip() for val in (raw or "").split(",") if val.strip()]


def _label_selector_match(labels: dict[str, Any], selector: str) -> bool:
    if not selector:
        return True
    labels = labels or {}
    for expr in _split_selector_terms(selector):
        if not expr:
            continue
        m_notin = re.match(r"^\s*([^\s!<>=(),]+)\s+notin\s+\(([^)]*)\)\s*$", expr)
        if m_notin:
            key = m_notin.group(1)
            values = _split_selector_values(m_notin.group(2))
            actual = labels.get(key)
            if actual is not None and str(actual) in values:
                return False
            continue
        m_in = re.match(r"^\s*([^\s!<>=(),]+)\s+in\s+\(([^)]*)\)\s*$", expr)
        if m_in:
            key = m_in.group(1)
            values = _split_selector_values(m_in.group(2))
            actual = labels.get(key)
            if actual is None or str(actual) not in values:
                return False
            continue
        if "!=" in expr:
            key, val = expr.split("!=", 1)
            key = key.strip()
            val = val.strip()
            actual = labels.get(key)
            if actual is not None and str(actual) == val:
                return False
            continue
        if "==" in expr:
            key, val = expr.split("==", 1)
            key = key.strip()
            val = val.strip()
            actual = labels.get(key)
            if actual is None or str(actual) != val:
                return False
            continue
        if "=" in expr:
            key, val = expr.split("=", 1)
            key = key.strip()
            val = val.strip()
            actual = labels.get(key)
            if actual is None or str(actual) != val:
                return False
            continue
        # Unsupported selector semantics -> best-effort pass
    return True


def _field_selector_match(name: str | None, namespace: str | None, selector: str) -> bool:
    if not selector:
        return True
    actual_name = name or ""
    actual_namespace = namespace or ""
    for expr in _split_selector_terms(selector):
        if not expr:
            continue
        op = None
        if "!=" in expr:
            key, val = expr.split("!=", 1)
            op = "!="
        elif "==" in expr:
            key, val = expr.split("==", 1)
            op = "=="
        elif "=" in expr:
            key, val = expr.split("=", 1)
            op = "="
        else:
            continue
        key = key.strip()
        val = val.strip()
        if key == "metadata.name":
            actual = actual_name
        elif key == "metadata.namespace":
            actual = actual_namespace
        else:
            # Unsupported field selector -> best-effort pass
            continue
        if op in {"=", "=="} and actual != val:
            return False
        if op == "!=" and actual == val:
            return False
    return True


def _selector_values_from_query(query: dict[str, list[str]]) -> tuple[str, str]:
    label_sel = (query.get("labelSelector", [""])[0] or "").strip()
    field_sel = (query.get("fieldSelector", [""])[0] or "").strip()
    return label_sel, field_sel


def _matches_selectors(obj: K8sObject | dict[str, Any], label_sel: str, field_sel: str) -> bool:
    if not label_sel and not field_sel:
        return True
    if isinstance(obj, K8sObject):
        meta = obj.metadata or {}
        labels = meta.get("labels") if isinstance(meta, dict) else None
        name = meta.get("name") if isinstance(meta, dict) else None
        namespace = meta.get("namespace") if isinstance(meta, dict) else None
        if not name:
            name = obj.name
        if namespace is None:
            namespace = obj.namespace
    else:
        meta = obj.get("metadata") if isinstance(obj, dict) else {}
        labels = meta.get("labels") if isinstance(meta, dict) else None
        name = meta.get("name") if isinstance(meta, dict) else None
        namespace = meta.get("namespace") if isinstance(meta, dict) else None
    labels = labels if isinstance(labels, dict) else {}
    if not _label_selector_match(labels, label_sel):
        return False
    return _field_selector_match(name, namespace, field_sel)


def _filter_k8s_items(items: list[K8sObject], label_sel: str, field_sel: str) -> list[K8sObject]:
    if not label_sel and not field_sel:
        return items
    return [item for item in items if _matches_selectors(item, label_sel, field_sel)]


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


def _ensure_managed_fields_entry(
    md: dict[str, Any],
    api_version: str,
    manager: str,
    operation: str,
    paths: set[str] | None = None,
) -> dict[str, Any]:
    managed = list(md.get("managedFields") or [])
    if any(m.get("manager") == manager for m in managed):
        return md
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017 - timezone-aware stamp
    paths = set(paths or {"*"})
    entry: dict[str, Any] = {
        "manager": manager,
        "operation": operation,
        "apiVersion": api_version,
        "time": now,
        "fieldsType": "FieldsV1",
        "paths": sorted(paths),
        "fieldsV1": _paths_to_fieldsV1(paths),
    }
    managed.append(entry)
    md["managedFields"] = managed
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
    # Note: this doc also powers the Swagger UI; keep descriptions concise but helpful.
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
                "status": {
                    "$ref": "#/definitions/io.k8s.api.autoscaling.v2.HorizontalPodAutoscalerStatus"
                },
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
                    "items": {
                        "$ref": "#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelectorRequirement"
                    },
                },
            },
            "additionalProperties": True,
        },
        "io.k8s.api.policy.v1.PodDisruptionBudgetSpec": {
            "type": "object",
            "properties": {
                "minAvailable": {"type": ["integer", "string"]},
                "maxUnavailable": {"type": ["integer", "string"]},
                "selector": {
                    "$ref": "#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.LabelSelector"
                },
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
        "io.k8s.api.ae.dev.v1alpha1.DeploymentSpec": {
            "type": "object",
            "properties": {
                "image": {"type": "string"},
                "command": {"type": "array", "items": {"type": "string"}},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "replicas": {"type": "integer"},
                "ports": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "health": {"type": "object", "additionalProperties": True},
                "service": {"type": "object", "additionalProperties": True},
                "ingress": {"type": "object", "additionalProperties": True},
                "resources": {"type": "object", "additionalProperties": True},
                "security": {"type": "object", "additionalProperties": True},
                "containers": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "initContainers": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "volumes": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "storage": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "emptyDirs": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "networkPolicy": {"type": "object", "additionalProperties": True},
                "exportHints": {"type": "object", "additionalProperties": True},
            },
            "additionalProperties": True,
        },
        "io.k8s.api.ae.dev.v1alpha1.Deployment": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                "spec": {"$ref": "#/definitions/io.k8s.api.ae.dev.v1alpha1.DeploymentSpec"},
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
    schemas.update(
        {
            "io.k8s.api.core.v1.Namespace": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "spec": {"type": "object", "additionalProperties": True},
                    "status": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.core.v1.Node": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "spec": {"type": "object", "additionalProperties": True},
                    "status": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.core.v1.Pod": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "spec": {"type": "object", "additionalProperties": True},
                    "status": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.core.v1.Endpoints": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "subsets": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.core.v1.Event": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "involvedObject": {"type": "object", "additionalProperties": True},
                    "reason": {"type": "string"},
                    "message": {"type": "string"},
                    "type": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.apps.v1.ReplicaSet": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "spec": {"type": "object", "additionalProperties": True},
                    "status": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.discovery.k8s.io.v1.EndpointSlice": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "addressType": {"type": "string"},
                    "endpoints": {"type": "array", "items": {"type": "object"}},
                    "ports": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.apiextensions.k8s.io.v1.CustomResourceDefinition": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"$ref": "#/definitions/io.k8s.api.meta.v1.ObjectMeta"},
                    "spec": {"type": "object", "additionalProperties": True},
                    "status": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.authorization.k8s.io.v1.SelfSubjectAccessReview": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "spec": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "io.k8s.api.authorization.k8s.io.v1.SelfSubjectRulesReview": {
                "type": "object",
                "properties": {
                    "apiVersion": {"type": "string"},
                    "kind": {"type": "string"},
                    "spec": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
        }
    )
    paths: dict[str, dict[str, Any]] = {}

    resource_hints: dict[str, dict[str, Any]] = {
        "namespaces": {
            "definition": "io.k8s.api.core.v1.Namespace",
            "example": {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "demo"},
            },
        },
        "configmaps": {
            "definition": "io.k8s.api.core.v1.ConfigMap",
            "example": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "demo-config", "namespace": "default"},
                "data": {"APP_MODE": "demo", "LOG_LEVEL": "info"},
            },
        },
        "secrets": {
            "definition": "io.k8s.api.core.v1.Secret",
            "example": {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "demo-secret", "namespace": "default"},
                "type": "Opaque",
                "data": {"TOKEN": "ZGVtby10b2tlbg=="},
            },
        },
        "serviceaccounts": {
            "definition": "io.k8s.api.core.v1.ServiceAccount",
            "example": {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "demo-sa", "namespace": "default"},
            },
        },
        "services": {
            "definition": "io.k8s.api.core.v1.Service",
            "example": {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "demo-svc", "namespace": "default"},
                "spec": {
                    "selector": {"app": "demo"},
                    "ports": [{"port": 80, "targetPort": 8080}],
                },
            },
        },
        "endpoints": {
            "definition": "io.k8s.api.core.v1.Endpoints",
            "example": {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {"name": "demo-svc", "namespace": "default"},
            },
        },
        "pods": {
            "definition": "io.k8s.api.core.v1.Pod",
            "example": {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "demo-pod", "namespace": "default"},
                "spec": {"containers": [{"name": "demo", "image": "nginx:latest"}]},
            },
        },
        "events": {
            "definition": "io.k8s.api.core.v1.Event",
            "example": {
                "apiVersion": "v1",
                "kind": "Event",
                "metadata": {"name": "demo.1", "namespace": "default"},
                "message": "Demo event",
                "type": "Normal",
            },
        },
        "deployments": {
            "definition": "io.k8s.api.apps.v1.Deployment",
            "example": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "demo"}},
                    "template": {
                        "metadata": {"labels": {"app": "demo"}},
                        "spec": {"containers": [{"name": "demo", "image": "nginx:latest"}]},
                    },
                },
            },
            "patch_example": {"spec": {"replicas": 2}},
        },
        "statefulsets": {
            "definition": "io.k8s.api.apps.v1.StatefulSet",
            "example": {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {
                    "serviceName": "demo",
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "demo"}},
                    "template": {
                        "metadata": {"labels": {"app": "demo"}},
                        "spec": {"containers": [{"name": "demo", "image": "nginx:latest"}]},
                    },
                },
            },
            "patch_example": {"spec": {"replicas": 2}},
        },
        "daemonsets": {
            "definition": "io.k8s.api.apps.v1.DaemonSet",
            "example": {
                "apiVersion": "apps/v1",
                "kind": "DaemonSet",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {
                    "selector": {"matchLabels": {"app": "demo"}},
                    "template": {
                        "metadata": {"labels": {"app": "demo"}},
                        "spec": {"containers": [{"name": "demo", "image": "nginx:latest"}]},
                    },
                },
            },
        },
        "replicasets": {
            "definition": "io.k8s.api.apps.v1.ReplicaSet",
            "example": {
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "demo"}},
                    "template": {
                        "metadata": {"labels": {"app": "demo"}},
                        "spec": {"containers": [{"name": "demo", "image": "nginx:latest"}]},
                    },
                },
            },
        },
        "jobs": {
            "definition": "io.k8s.api.batch.v1.Job",
            "example": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "demo-job", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "demo", "image": "busybox"}],
                        }
                    }
                },
            },
        },
        "cronjobs": {
            "definition": "io.k8s.api.batch.v1.CronJob",
            "example": {
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "demo-cron", "namespace": "default"},
                "spec": {
                    "schedule": "*/5 * * * *",
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "restartPolicy": "Never",
                                    "containers": [
                                        {"name": "demo", "image": "busybox", "args": ["echo", "hi"]}
                                    ],
                                }
                            }
                        }
                    },
                },
            },
        },
        "ingresses": {
            "definition": "io.k8s.api.networking.v1.Ingress",
            "example": {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": "demo-ingress", "namespace": "default"},
                "spec": {
                    "rules": [
                        {
                            "host": "demo.local",
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": "demo-svc",
                                                "port": {"number": 80},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
        },
        "roles": {
            "definition": "io.k8s.api.rbac.v1.Role",
            "example": {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "demo-role", "namespace": "default"},
            },
        },
        "rolebindings": {
            "definition": "io.k8s.api.rbac.v1.RoleBinding",
            "example": {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "demo-rb", "namespace": "default"},
            },
        },
        "clusterroles": {
            "definition": "io.k8s.api.rbac.v1.ClusterRole",
            "example": {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "demo-cr"},
            },
        },
        "clusterrolebindings": {
            "definition": "io.k8s.api.rbac.v1.ClusterRoleBinding",
            "example": {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "demo-crb"},
            },
        },
        "poddisruptionbudgets": {
            "definition": "io.k8s.api.policy.v1.PodDisruptionBudget",
            "example": {
                "apiVersion": "policy/v1",
                "kind": "PodDisruptionBudget",
                "metadata": {"name": "demo-pdb", "namespace": "default"},
                "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "demo"}}},
            },
        },
        "horizontalpodautoscalers": {
            "definition": "io.k8s.api.autoscaling.v2.HorizontalPodAutoscaler",
            "example": {
                "apiVersion": "autoscaling/v2",
                "kind": "HorizontalPodAutoscaler",
                "metadata": {"name": "demo-hpa", "namespace": "default"},
                "spec": {"minReplicas": 1, "maxReplicas": 3},
            },
        },
        "customresourcedefinitions": {
            "definition": "io.k8s.api.apiextensions.k8s.io.v1.CustomResourceDefinition",
            "example": {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": "apps.ae.dev"},
            },
        },
        "endpointslices": {
            "definition": "io.k8s.api.discovery.k8s.io.v1.EndpointSlice",
            "example": {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSlice",
                "metadata": {"name": "demo-slice", "namespace": "default"},
            },
        },
        "apps": {
            "definition": "io.k8s.api.ae.dev.v1alpha1.Deployment",
            "example": {
                "apiVersion": "ae.dev/v1alpha1",
                "kind": "Deployment",
                "metadata": {"name": "demo", "namespace": "default"},
                "spec": {"image": "nginx:latest", "ports": [{"containerPort": 8080}]},
            },
        },
    }

    def _op(
        *,
        tag: str,
        summary: str,
        description: str,
        parameters: list[dict[str, Any]] | None = None,
        responses: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        op: dict[str, Any] = {
            "tags": [tag],
            "summary": summary,
            "description": description,
            "responses": responses or {"200": {"description": "OK"}},
        }
        if parameters:
            op["parameters"] = parameters
        return op

    def _add_path(path: str, ops: dict[str, dict[str, Any]]) -> None:
        if not ops:
            return
        entry = paths.setdefault(path, {})
        for method, op in ops.items():
            entry.setdefault(method, op)

    def _path_param(name: str, description: str) -> dict[str, Any]:
        return {
            "name": name,
            "in": "path",
            "required": True,
            "type": "string",
            "description": description,
        }

    def _query_param(
        name: str, description: str, param_type: str, default: Any | None = None
    ) -> dict[str, Any]:
        param: dict[str, Any] = {
            "name": name,
            "in": "query",
            "type": param_type,
            "description": description,
        }
        if default is not None:
            param["default"] = default
        return param

    def _body_param(description: str) -> dict[str, Any]:
        return {
            "name": "body",
            "in": "body",
            "required": True,
            "description": description,
            "schema": {"type": "object"},
        }

    def _tag_for_base(base: str) -> str:
        return {
            "/api/v1": "core/v1",
            "/apis/apps/v1": "apps/v1",
            "/apis/batch/v1": "batch/v1",
            "/apis/networking.k8s.io/v1": "networking.k8s.io/v1",
            "/apis/rbac.authorization.k8s.io/v1": "rbac.authorization.k8s.io/v1",
            "/apis/authorization.k8s.io/v1": "authorization.k8s.io/v1",
            "/apis/policy/v1": "policy/v1",
            "/apis/autoscaling/v2": "autoscaling/v2",
            "/apis/apiextensions.k8s.io/v1": "apiextensions.k8s.io/v1",
            "/apis/discovery.k8s.io/v1": "discovery.k8s.io/v1",
            "/apis/storage.k8s.io/v1": "storage.k8s.io/v1",
            "/apis/snapshot.storage.k8s.io/v1": "snapshot.storage.k8s.io/v1",
            "/apis/ae.dev/v1alpha1": "ae.dev/v1alpha1",
        }.get(base, "discovery")

    def _list_params(namespaced: bool) -> list[dict[str, Any]]:
        params: list[dict[str, Any]] = []
        if namespaced:
            params.append(_path_param("namespace", "Namespace name"))
        params.extend(
            [
                _query_param("limit", "Maximum number of items to return", "integer", 100),
                _query_param("continue", "Continue token for pagination", "string"),
                _query_param("labelSelector", "Label selector to filter results", "string"),
                _query_param("fieldSelector", "Field selector to filter results", "string"),
                _query_param(
                    "watch",
                    "Set to true to stream watch events instead of a list",
                    "boolean",
                ),
                _query_param("timeoutSeconds", "Watch timeout in seconds", "integer"),
            ]
        )
        return params

    def _item_params(namespaced: bool) -> list[dict[str, Any]]:
        params = []
        if namespaced:
            params.append(_path_param("namespace", "Namespace name"))
        params.append(_path_param("name", "Resource name"))
        return params

    def _schema_for(plural: str) -> dict[str, Any] | None:
        hint = resource_hints.get(plural)
        if not hint:
            return None
        definition = hint.get("definition")
        if not definition:
            return None
        return {"$ref": f"#/definitions/{definition}"}

    def _example_for(plural: str) -> dict[str, Any] | None:
        hint = resource_hints.get(plural)
        if not hint:
            return None
        return hint.get("example")

    def _patch_example_for(plural: str) -> dict[str, Any] | None:
        hint = resource_hints.get(plural)
        if not hint:
            return None
        return hint.get("patch_example")

    def _list_schema(item_schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item_schema:
            return None
        return {
            "type": "object",
            "properties": {"items": {"type": "array", "items": item_schema}},
            "additionalProperties": True,
        }

    def _list_example(example: dict[str, Any] | None) -> dict[str, Any] | None:
        if not example:
            return None
        kind = str(example.get("kind") or "List")
        api_version = example.get("apiVersion")
        payload: dict[str, Any] = {"kind": f"{kind}List", "items": [example]}
        if api_version:
            payload["apiVersion"] = api_version
        return payload

    def _response_ok(
        schema: dict[str, Any] | None = None, example: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp: dict[str, Any] = {"description": "OK"}
        if schema:
            resp["schema"] = schema
        if example is not None:
            resp["examples"] = {"application/json": example}
        return resp

    def _describe_list(plural: str, namespaced: bool) -> str:
        scope = "namespace" if namespaced else "cluster"
        example = f"kubectl get {plural} -n <namespace>" if namespaced else f"kubectl get {plural}"
        return (
            f"List {plural} in the {scope} scope. Use `watch=1` to stream changes."
            f"\n\nExample:\n\n`{example}`"
        )

    def _describe_get(plural: str, namespaced: bool) -> str:
        example = (
            f"kubectl get {plural} <name> -n <namespace> -o yaml"
            if namespaced
            else f"kubectl get {plural} <name> -o yaml"
        )
        return f"Get a single {plural.rstrip('s')} by name.\n\nExample:\n\n`{example}`"

    def _describe_create(plural: str) -> str:
        return (
            f"Create a new {plural.rstrip('s')} from a manifest."
            "\n\nExample:\n\n`kubectl apply -f <manifest.yaml>`"
        )

    def _describe_update(plural: str) -> str:
        return (
            f"Replace a {plural.rstrip('s')} by name."
            "\n\nExample:\n\n`kubectl apply -f <manifest.yaml>`"
        )

    def _describe_patch(plural: str) -> str:
        return (
            f"Patch a {plural.rstrip('s')} by name."
            "\n\nExample:\n\n`kubectl patch "
            f"{plural.rstrip('s')} <name> -p '{{\"spec\":{{}}}}'`"
        )

    def _describe_delete(plural: str) -> str:
        return (
            f"Delete a {plural.rstrip('s')} by name."
            "\n\nExample:\n\n`kubectl delete "
            f"{plural.rstrip('s')} <name>`"
        )

    def _add_resource(base: str, plural: str, namespaced: bool, verbs: set[str]) -> None:
        tag = _tag_for_base(base)
        item_schema = _schema_for(plural)
        list_schema = _list_schema(item_schema)
        example = _example_for(plural)
        list_example = _list_example(example)
        patch_example = _patch_example_for(plural)

        def _body_for(
            description: str,
            *,
            example_override: dict[str, Any] | None = None,
            schema_override: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            param = _body_param(description)
            schema = schema_override or item_schema
            if schema:
                param["schema"] = schema
            ex = example_override if example_override is not None else example
            if ex is not None:
                param["x-example"] = ex
            return param

        list_methods: list[str] = []
        if "list" in verbs or "watch" in verbs:
            list_methods.append("get")
        if "create" in verbs:
            list_methods.append("post")
        if namespaced:
            ns_path = f"{base}/namespaces/{{namespace}}/{plural}"
            list_ops: dict[str, dict[str, Any]] = {}
            if "get" in list_methods:
                list_ops["get"] = _op(
                    tag=tag,
                    summary=f"List {plural}",
                    description=_describe_list(plural, namespaced=True),
                    parameters=_list_params(namespaced=True),
                    responses={"200": _response_ok(list_schema, list_example)},
                )
            if "post" in list_methods:
                list_ops["post"] = _op(
                    tag=tag,
                    summary=f"Create {plural.rstrip('s')}",
                    description=_describe_create(plural),
                    parameters=[
                        _path_param("namespace", "Namespace name"),
                        _body_for("Resource body"),
                    ],
                    responses={"200": _response_ok(item_schema, example)},
                )
            _add_path(ns_path, list_ops)
            if "list" in verbs or "watch" in verbs:
                _add_path(
                    f"{base}/{plural}",
                    {
                        "get": _op(
                            tag=tag,
                            summary=f"List {plural} across all namespaces",
                            description=_describe_list(plural, namespaced=False),
                            parameters=_list_params(namespaced=False),
                            responses={"200": _response_ok(list_schema, list_example)},
                        )
                    },
                )
            item_path = f"{base}/namespaces/{{namespace}}/{plural}/{{name}}"
        else:
            list_ops = {}
            if "get" in list_methods:
                list_ops["get"] = _op(
                    tag=tag,
                    summary=f"List {plural}",
                    description=_describe_list(plural, namespaced=False),
                    parameters=_list_params(namespaced=False),
                    responses={"200": _response_ok(list_schema, list_example)},
                )
            if "post" in list_methods:
                list_ops["post"] = _op(
                    tag=tag,
                    summary=f"Create {plural.rstrip('s')}",
                    description=_describe_create(plural),
                    parameters=[_body_for("Resource body")],
                    responses={"200": _response_ok(item_schema, example)},
                )
            _add_path(f"{base}/{plural}", list_ops)
            item_path = f"{base}/{plural}/{{name}}"
        item_methods: list[str] = []
        if "get" in verbs:
            item_methods.append("get")
        if "update" in verbs:
            item_methods.append("put")
        if "patch" in verbs:
            item_methods.append("patch")
        if "delete" in verbs:
            item_methods.append("delete")
        item_ops: dict[str, dict[str, Any]] = {}
        if "get" in item_methods:
            item_ops["get"] = _op(
                tag=tag,
                summary=f"Get {plural.rstrip('s')}",
                description=_describe_get(plural, namespaced),
                parameters=_item_params(namespaced),
                responses={"200": _response_ok(item_schema, example)},
            )
        if "put" in item_methods:
            item_ops["put"] = _op(
                tag=tag,
                summary=f"Replace {plural.rstrip('s')}",
                description=_describe_update(plural),
                parameters=_item_params(namespaced) + [_body_for("Resource body")],
                responses={"200": _response_ok(item_schema, example)},
            )
        if "patch" in item_methods:
            item_ops["patch"] = _op(
                tag=tag,
                summary=f"Patch {plural.rstrip('s')}",
                description=_describe_patch(plural),
                parameters=_item_params(namespaced)
                + [_body_for("Patch body", example_override=patch_example or {"spec": {}})],
                responses={"200": _response_ok(item_schema, example)},
            )
        if "delete" in item_methods:
            item_ops["delete"] = _op(
                tag=tag,
                summary=f"Delete {plural.rstrip('s')}",
                description=_describe_delete(plural),
                parameters=_item_params(namespaced),
            )
        _add_path(item_path, item_ops)

    def _add_subresource(
        base: str, plural: str, subresource: str, namespaced: bool, verbs: set[str]
    ) -> None:
        tag = _tag_for_base(base)
        if namespaced:
            path = f"{base}/namespaces/{{namespace}}/{plural}/{{name}}/{subresource}"
        else:
            path = f"{base}/{plural}/{{name}}/{subresource}"
        methods: list[str] = []
        if "get" in verbs:
            methods.append("get")
        if "create" in verbs:
            methods.append("post")
        if "update" in verbs:
            methods.append("put")
        if "patch" in verbs:
            methods.append("patch")
        if "delete" in verbs:
            methods.append("delete")
        params = _item_params(namespaced)
        desc = f"{plural.rstrip('s')} {subresource} subresource."
        ops: dict[str, dict[str, Any]] = {}
        if "get" in methods:
            ops["get"] = _op(
                tag=tag,
                summary=f"Get {plural.rstrip('s')} {subresource}",
                description=desc,
                parameters=params,
            )
        if "post" in methods:
            ops["post"] = _op(
                tag=tag,
                summary=f"Create {plural.rstrip('s')} {subresource}",
                description=desc,
                parameters=params + [_body_param("Subresource body")],
            )
        if "put" in methods:
            ops["put"] = _op(
                tag=tag,
                summary=f"Update {plural.rstrip('s')} {subresource}",
                description=desc,
                parameters=params + [_body_param("Subresource body")],
            )
        if "patch" in methods:
            ops["patch"] = _op(
                tag=tag,
                summary=f"Patch {plural.rstrip('s')} {subresource}",
                description=desc,
                parameters=params + [_body_param("Subresource body")],
            )
        if "delete" in methods:
            ops["delete"] = _op(
                tag=tag,
                summary=f"Delete {plural.rstrip('s')} {subresource}",
                description=desc,
                parameters=params,
            )
        _add_path(path, ops)

    # Non-resource endpoints and discovery
    discovery_tag = "discovery"
    for p in (
        "/api",
        "/apis",
        "/version",
        "/healthz",
        "/readyz",
        "/metrics",
        "/openapi/v2",
        "/openapi/v3",
        "/swagger.json",
        "/api/v1",
        "/apis/apps/v1",
        "/apis/batch/v1",
        "/apis/networking.k8s.io/v1",
        "/apis/rbac.authorization.k8s.io/v1",
        "/apis/authorization.k8s.io/v1",
        "/apis/policy/v1",
        "/apis/autoscaling/v2",
        "/apis/apiextensions.k8s.io/v1",
        "/apis/discovery.k8s.io/v1",
        "/apis/storage.k8s.io/v1",
        "/apis/snapshot.storage.k8s.io/v1",
        "/apis/ae.dev/v1alpha1",
    ):
        _add_path(
            p,
            {
                "get": _op(
                    tag=discovery_tag,
                    summary=f"GET {p}",
                    description="Kubernetes discovery or health endpoint.",
                    parameters=[],
                )
            },
        )

    # Core API resources
    for plural, namespaced, verbs in (
        (
            "namespaces",
            False,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        (
            "configmaps",
            True,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        ("secrets", True, {"get", "list", "watch", "create", "delete", "patch", "update"}),
        (
            "persistentvolumeclaims",
            True,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        (
            "persistentvolumes",
            False,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        (
            "serviceaccounts",
            True,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        ("services", True, {"get", "list", "watch", "create", "delete", "patch", "update"}),
        ("endpoints", True, {"get", "list"}),
        ("nodes", False, {"get", "list"}),
        ("pods", True, {"get", "list", "watch"}),
        ("events", True, {"get", "list", "watch"}),
    ):
        _add_resource("/api/v1", plural, namespaced, verbs)

    # Core subresources and special endpoints
    _add_path(
        "/api/v1/namespaces/{namespace}/pods/{name}/log",
        {
            "get": _op(
                tag="core/v1",
                summary="Stream pod logs",
                description="Tail logs for a specific pod (similar to `kubectl logs`).",
                parameters=_item_params(True)
                + [
                    _query_param("container", "Container name", "string"),
                    _query_param("tailLines", "Number of lines from the end", "integer"),
                    _query_param("follow", "Follow the stream", "boolean"),
                    _query_param("timestamps", "Include timestamps", "boolean"),
                ],
            )
        },
    )
    _add_path(
        "/api/v1/namespaces/{namespace}/pods/{name}/exec",
        {
            "get": _op(
                tag="core/v1",
                summary="Exec into a pod (GET)",
                description="Exec streaming endpoint (SPDY/WebSocket). Used by `kubectl exec`.",
                parameters=_item_params(True),
            ),
            "post": _op(
                tag="core/v1",
                summary="Exec into a pod (POST)",
                description="Exec streaming endpoint (SPDY/WebSocket). Used by `kubectl exec`.",
                parameters=_item_params(True),
            ),
        },
    )
    _add_path(
        "/api/v1/namespaces/{namespace}/pods/{name}/portforward",
        {
            "get": _op(
                tag="core/v1",
                summary="Port-forward to a pod (GET)",
                description="Port-forward streaming endpoint (SPDY/WebSocket).",
                parameters=_item_params(True),
            ),
            "post": _op(
                tag="core/v1",
                summary="Port-forward to a pod (POST)",
                description="Port-forward streaming endpoint (SPDY/WebSocket).",
                parameters=_item_params(True),
            ),
        },
    )
    _add_path(
        "/api/v1/namespaces/{namespace}/services/{name}/portforward",
        {
            "get": _op(
                tag="core/v1",
                summary="Port-forward to a service (GET)",
                description="Port-forward streaming endpoint (SPDY/WebSocket).",
                parameters=_item_params(True),
            ),
            "post": _op(
                tag="core/v1",
                summary="Port-forward to a service (POST)",
                description="Port-forward streaming endpoint (SPDY/WebSocket).",
                parameters=_item_params(True),
            ),
        },
    )
    _add_path(
        "/api/v1/events/{name}",
        {
            "get": _op(
                tag="core/v1",
                summary="Get event by name",
                description="Fetch a single event by name.",
                parameters=_item_params(False),
            )
        },
    )

    # apps/v1 resources
    for plural, verbs in (
        (
            "deployments",
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        (
            "statefulsets",
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        (
            "daemonsets",
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        ),
        ("replicasets", {"get", "list", "watch"}),
    ):
        _add_resource("/apis/apps/v1", plural, True, verbs)

    for plural, subresource, verbs in (
        ("deployments", "status", {"get"}),
        ("deployments", "scale", {"get", "update"}),
        ("statefulsets", "status", {"get"}),
        ("daemonsets", "status", {"get"}),
    ):
        _add_subresource("/apis/apps/v1", plural, subresource, True, verbs)

    # batch/v1 resources
    for plural, verbs in (
        ("jobs", {"get", "list", "watch", "create", "delete", "patch", "update"}),
        ("cronjobs", {"get", "list", "watch", "create", "delete", "patch", "update"}),
    ):
        _add_resource("/apis/batch/v1", plural, True, verbs)
    for plural, verbs in (("jobs", {"get"}), ("cronjobs", {"get"})):
        _add_subresource("/apis/batch/v1", plural, "status", True, verbs)

    # networking.k8s.io/v1 resources
    _add_resource(
        "/apis/networking.k8s.io/v1",
        "ingresses",
        True,
        {"get", "list", "watch", "create", "delete", "update"},
    )

    # rbac.authorization.k8s.io/v1 resources
    for plural, namespaced in (
        ("roles", True),
        ("rolebindings", True),
        ("clusterroles", False),
        ("clusterrolebindings", False),
    ):
        _add_resource(
            "/apis/rbac.authorization.k8s.io/v1",
            plural,
            namespaced,
            {"get", "list", "watch", "create", "delete", "patch", "update"},
        )

    # authorization.k8s.io/v1 resources (create-only)
    for plural in (
        "subjectaccessreviews",
        "selfsubjectaccessreviews",
        "selfsubjectrulesreviews",
    ):
        _add_resource("/apis/authorization.k8s.io/v1", plural, False, {"create"})

    # policy/v1 resources
    _add_resource(
        "/apis/policy/v1",
        "poddisruptionbudgets",
        True,
        {"get", "list", "watch", "create", "delete", "patch", "update"},
    )

    # autoscaling/v2 resources
    _add_resource(
        "/apis/autoscaling/v2",
        "horizontalpodautoscalers",
        True,
        {"get", "list", "watch", "create", "delete", "patch", "update"},
    )

    # apiextensions.k8s.io/v1 resources
    _add_resource(
        "/apis/apiextensions.k8s.io/v1",
        "customresourcedefinitions",
        False,
        {"get", "list", "watch", "create", "delete", "update"},
    )

    # discovery.k8s.io/v1 resources
    _add_resource(
        "/apis/discovery.k8s.io/v1",
        "endpointslices",
        True,
        {"get", "list", "watch"},
    )

    # ae.dev/v1alpha1 resources
    _add_resource(
        "/apis/ae.dev/v1alpha1",
        "apps",
        True,
        {"get", "list", "watch", "create", "delete", "patch", "update"},
    )
    doc = {
        "swagger": "2.0",
        "info": {
            "title": "k1s apishim",
            "version": "0.1.3.dev0",
            "description": (
                "Kubernetes-compatible API shim for local k1s development. "
                "Supports discovery, basic CRUD for core workloads, and a minimal OpenAPI schema "
                "for kubectl/helm compatibility.\n\n"
                "Usage tips:\n"
                "- Use `/openapi/v3` for schema validation.\n"
                "- Use `watch=1` on list endpoints to stream changes.\n"
                "- Exec/port-forward endpoints use SPDY/WebSocket streaming."
            ),
        },
        "produces": ["application/json"],
        "schemes": ["http"],
        "paths": paths,
        "definitions": schemas,
        "tags": [
            {"name": "discovery", "description": "Discovery and non-resource endpoints"},
            {"name": "core/v1", "description": "Core API resources"},
            {"name": "apps/v1", "description": "Workload resources (apps)"},
            {"name": "batch/v1", "description": "Batch resources (jobs/cronjobs)"},
            {"name": "networking.k8s.io/v1", "description": "Networking resources"},
            {"name": "rbac.authorization.k8s.io/v1", "description": "RBAC resources"},
            {"name": "authorization.k8s.io/v1", "description": "Authorization reviews"},
            {"name": "policy/v1", "description": "Policy resources"},
            {"name": "autoscaling/v2", "description": "Autoscaling resources"},
            {
                "name": "apiextensions.k8s.io/v1",
                "description": "CustomResourceDefinitions",
            },
            {"name": "discovery.k8s.io/v1", "description": "EndpointSlices"},
            {"name": "storage.k8s.io/v1", "description": "Storage resources"},
            {
                "name": "snapshot.storage.k8s.io/v1",
                "description": "Volume snapshot resources",
            },
            {"name": "ae.dev/v1alpha1", "description": "k1s custom resources"},
        ],
    }
    return doc


def _openapi_v3_stub() -> dict[str, Any]:
    doc = _swagger_doc()
    return {
        "openapi": "3.0.0",
        "info": doc.get("info", {}),
        "paths": doc.get("paths", {}),
        "components": {"schemas": doc.get("definitions", {})},
        "tags": doc.get("tags", []),
        "x-k1s-note": "OpenAPI v3 mirrors /openapi/v2 and is kept authoritative alongside it",
    }


@dataclass
class Principal:
    username: str
    groups: set[str]
    token_role: str | None
    token: str | None
    scopes: list[str] | None = None


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "k1s-apishim"
    admin_token: str | None = os.getenv("AE_APISHIM_TOKEN")
    read_token: str | None = os.getenv("AE_APISHIM_READ_TOKEN")
    exec_token: str | None = os.getenv("AE_APISHIM_EXEC_TOKEN")
    portforward_token: str | None = os.getenv("AE_APISHIM_PORTFORWARD_TOKEN")
    mint_token: str | None = os.getenv("AE_APISHIM_MINT_TOKEN")
    session_secret: str | None = os.getenv("AE_APISHIM_SESSION_SECRET")
    pod_state_check: bool = os.getenv("AE_APISHIM_POD_STATE_CHECK", "0") == "1"
    pod_watch_check: bool = False
    pod_watch_ttl: float = 30.0
    pod_watch_cache: dict[tuple[str, str], tuple[str | None, int, float]] = {}
    pod_watch_lock = threading.RLock()
    allow_anonymous: bool = os.getenv("AE_APISHIM_ALLOW_ANON", "0") == "1"
    rbac_enabled: bool = os.getenv("AE_APISHIM_RBAC", "0") == "1"
    rbac_eval_roles: bool = os.getenv("AE_APISHIM_RBAC_EVAL", "0") == "1"
    app_admission_mode: str = os.getenv("AE_APISHIM_APP_ADMISSION", "enforce")
    sa_tokens: dict[str, tuple[str, str, float]] = {}
    sa_tokens_lock = threading.RLock()
    sa_token_ttl: int = int(os.getenv("AE_APISHIM_SA_TOKEN_TTL", "3600") or "3600")
    # Simple in-memory RBAC rules: (verb, resource) -> allowed roles
    rbac_policies: dict[tuple[str, str], set[str]] = {
        ("get", "*"): {"admin", "read"},
        ("list", "*"): {"admin", "read"},
        ("watch", "*"): {"admin", "read"},
        ("create", "*"): {"admin"},
        ("create", "pods/exec"): {"admin", "exec"},
        ("create", "pods/attach"): {"admin", "exec"},
        ("create", "pods/portforward"): {"admin", "portforward"},
        ("create", "services/portforward"): {"admin", "portforward"},
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
    _crd_refresh_monotonic: float = 0.0

    def _app_admission_mode(self) -> str:
        mode = (self.app_admission_mode or "enforce").strip().lower()
        if mode in {"warn", "warning"}:
            return "warn"
        if mode in {"off", "disabled", "ignore"}:
            return "off"
        return "enforce"

    def _add_warning(self, message: str) -> None:
        if not message:
            return
        warnings = getattr(self, "_warnings", None)
        if warnings is None:
            warnings = []
            self._warnings = warnings
        warnings.append(message)

    def _emit_warnings(self) -> None:
        warnings = getattr(self, "_warnings", None)
        if not warnings:
            return
        for msg in warnings:
            safe = str(msg).replace('"', "'")
            self.send_header("Warning", f'299 - "{safe}"')

    @classmethod
    def rehydrate_sa_tokens(cls, store: ObjectStore) -> None:
        try:
            accounts = store.list_all("", "v1", "serviceaccounts")
        except Exception:
            return
        now = time.time()
        tokens: dict[str, tuple[str, str, float]] = {}
        for obj in accounts:
            md = obj.metadata or {}
            anns = md.get("annotations") or {}
            tok = anns.get("ae.apishim/token")
            if not tok:
                continue
            try:
                exp = float(anns.get("ae.apishim/token-exp", "0") or 0)
            except Exception:
                exp = 0
            if exp <= now:
                continue
            tokens[tok] = (obj.namespace or "default", obj.name, exp)
        if tokens:
            with cls.sa_tokens_lock:
                cls.sa_tokens.update(tokens)

    def _lookup_sa_token(self, token: str) -> tuple[str, str, float] | None:
        if not token:
            return None
        try:
            accounts = self.server.store.list_all("", "v1", "serviceaccounts")  # type: ignore[attr-defined]
        except Exception:
            return None
        now = time.time()
        for obj in accounts:
            md = obj.metadata or {}
            anns = md.get("annotations") or {}
            if anns.get("ae.apishim/token") != token:
                continue
            try:
                exp = float(anns.get("ae.apishim/token-exp", "0") or 0)
            except Exception:
                exp = 0
            if exp <= now:
                return None
            return (obj.namespace or "default", obj.name, exp)
        return None

    def _parse_session_token(self, token: str) -> tuple[str, list[str]] | None:
        if not token:
            return None
        secret = self.session_secret or os.getenv("AE_APISHIM_SESSION_SECRET")
        if not secret:
            return None
        if not token.startswith("sess1."):
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        sig_b64 = parts[2]

        def _b64url_decode(val: str) -> bytes:
            pad = "=" * (-len(val) % 4)
            return base64.urlsafe_b64decode((val + pad).encode("utf-8"))

        try:
            payload_raw = _b64url_decode(payload_b64)
            sig_raw = _b64url_decode(sig_b64)
        except Exception:
            return None
        try:
            expected = hmac.new(
                secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(sig_raw, expected):
                return None
        except Exception:
            return None
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except Exception:
            return None
        try:
            exp = float(payload.get("exp") or 0)
        except Exception:
            exp = 0
        if exp <= time.time():
            return None
        role = str(payload.get("role") or "").strip().lower()
        if role not in {"exec", "portforward", "read"}:
            return None
        scopes_val = payload.get("scopes") or payload.get("scope") or []
        scopes: list[str] = []
        if isinstance(scopes_val, str):
            scopes = [scopes_val]
        elif isinstance(scopes_val, list | tuple):
            scopes = [str(s) for s in scopes_val if s]
        return role, scopes

    def _mint_session_token(
        self,
        *,
        role: str,
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        secret = self.session_secret or os.getenv("AE_APISHIM_SESSION_SECRET", "")
        secret = str(secret).strip()
        if not secret:
            raise RuntimeError("session tokens disabled")
        role_name = str(role or "").strip().lower()
        if role_name not in {"exec", "portforward", "read"}:
            raise ValueError("invalid role for session token")
        clean_scopes = [str(s).strip() for s in (scopes or []) if str(s).strip()]
        try:
            default_ttl = int(os.getenv("AE_APISHIM_SESSION_TTL", "600") or "600")
        except Exception:
            default_ttl = 600
        try:
            max_ttl = int(os.getenv("AE_APISHIM_SESSION_TTL_MAX", str(default_ttl)) or default_ttl)
        except Exception:
            max_ttl = default_ttl
        if default_ttl <= 0:
            default_ttl = 600
        if max_ttl <= 0:
            max_ttl = default_ttl
        ttl = int(ttl_seconds) if ttl_seconds and int(ttl_seconds) > 0 else int(default_ttl)
        ttl = max(60, min(int(ttl), int(max_ttl)))
        exp = int(time.time()) + int(ttl)
        token_payload: dict[str, Any] = {"role": role_name, "exp": exp}
        if clean_scopes:
            token_payload["scopes"] = clean_scopes
        payload_raw = json.dumps(token_payload, separators=(",", ":")).encode("utf-8")

        def _b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        payload_b64 = _b64url(payload_raw)
        sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        sig_b64 = _b64url(sig)
        token = f"sess1.{payload_b64}.{sig_b64}"
        return {
            "token": token,
            "expires_in": ttl,
            "expires_at": exp,
            "role": role_name,
            "scopes": clean_scopes,
        }

    def _parse_principal(self) -> Principal:
        cached = getattr(self, "_principal_cache", None)
        if cached is not None:
            return cached
        hdr = self.headers.get("Authorization", "")
        tok = hdr[7:] if hdr.startswith("Bearer ") else ""
        if not tok:
            try:
                if (self.headers.get("Upgrade") or "").lower() == "websocket":
                    tok = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            except Exception:
                tok = tok or ""
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
        elif tok and tok == self.exec_token:
            username = "exec"
            groups = {"system:authenticated", "exec"}
            token_role = "exec"  # noqa: S105 - role label, not a secret
        elif tok and tok == self.portforward_token:
            username = "portforward"
            groups = {"system:authenticated", "portforward"}
            token_role = "portforward"  # noqa: S105 - role label, not a secret
        elif tok and tok == self.mint_token:
            username = "mint"
            groups = {"system:authenticated", "mint"}
            token_role = "mint"  # noqa: S105 - role label, not a secret
        else:
            session = self._parse_session_token(tok)
            if session:
                role, scopes = session
                username = f"session:{role}"
                groups = {"system:authenticated", role}
                token_role = role
                principal = Principal(
                    username=username,
                    groups=groups,
                    token_role=token_role,
                    token=tok,
                    scopes=scopes,
                )
                self._principal_cache = principal
                return principal
            with self.sa_tokens_lock:
                sa = self.sa_tokens.get(tok)
            if sa is None and tok:
                sa = self._lookup_sa_token(tok)
                if sa:
                    with self.sa_tokens_lock:
                        self.sa_tokens[tok] = sa
            if sa:
                ns, name, exp_ts = sa
                if exp_ts < time.time():
                    # expired; drop it
                    with self.sa_tokens_lock:
                        self.sa_tokens.pop(tok, None)
                    principal = Principal(
                        username="system:unauthenticated",
                        groups={"system:unauthenticated"},
                        token_role=None,
                        token=None,
                    )
                    self._principal_cache = principal
                    return principal
                username = f"system:serviceaccount:{ns}:{name}"
                groups = {
                    "system:authenticated",
                    "system:serviceaccounts",
                    f"system:serviceaccounts:{ns}",
                }
                token_role = None
        principal = Principal(username=username, groups=groups, token_role=token_role, token=tok)
        self._principal_cache = principal
        return principal

    def _stream_limits(self) -> tuple[float | None, float | None]:
        try:
            max_seconds = float(os.getenv("AE_APISHIM_STREAM_MAX_SECONDS", "0") or 0)
        except Exception:
            max_seconds = 0
        try:
            idle_seconds = float(os.getenv("AE_APISHIM_STREAM_IDLE_SECONDS", "0") or 0)
        except Exception:
            idle_seconds = 0
        max_v = max_seconds if max_seconds > 0 else None
        idle_v = idle_seconds if idle_seconds > 0 else None
        return max_v, idle_v

    def _stream_byte_limit(self) -> int | None:
        try:
            max_bytes = int(os.getenv("AE_APISHIM_STREAM_MAX_BYTES", "0") or 0)
        except Exception:
            max_bytes = 0
        return max_bytes if max_bytes > 0 else None

    def _cri_pf_enabled(self) -> bool:
        raw = str(os.getenv("AE_APISHIM_CRI_PORTFORWARD", "")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _cri_pf_force(self) -> bool:
        raw = str(os.getenv("AE_APISHIM_CRI_PORTFORWARD_FORCE", "")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _alloc_local_port() -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _wait_local_port(self, port: int, proc: subprocess.Popen, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
                    return True
            except Exception:
                time.sleep(0.05)
        return False

    def _start_cri_port_forward(
        self, pod_id: str, ports: list[int]
    ) -> tuple[dict[int, int], list[subprocess.Popen]]:
        crictl = os.getenv("CRICTL_BIN", "crictl")
        if shutil.which(crictl) is None:
            LOGGER.warning("cri port-forward requested but crictl not found")
            return {}, []
        endpoint = os.getenv("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock")
        port_map: dict[int, int] = {}
        procs: list[subprocess.Popen] = []
        entries: list[tuple[int, int, subprocess.Popen]] = []
        for port in ports:
            local_port = self._alloc_local_port()
            args = [
                crictl,
                "--runtime-endpoint",
                endpoint,
                "port-forward",
                str(pod_id),
                f"{local_port}:{int(port)}",
            ]
            proc = subprocess.Popen(
                args,  # noqa: S603 - fixed args, no user input
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            port_map[int(port)] = int(local_port)
            procs.append(proc)
            entries.append((int(port), int(local_port), proc))
        for rport, lport, proc in entries:
            if not self._wait_local_port(lport, proc):
                LOGGER.warning("cri port-forward failed to bind local port for %s", rport)
                self._stop_cri_port_forward(procs)
                return {}, []
        return port_map, procs

    @staticmethod
    def _stop_cri_port_forward(procs: list[subprocess.Popen]) -> None:
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @staticmethod
    def _app_from_labels(labels: dict[str, Any]) -> str | None:
        for key in ("ae.app", "app.kubernetes.io/name", "app"):
            val = labels.get(key)
            if val:
                return str(val)
        return None

    @staticmethod
    def _normalize_runtime_endpoint(endpoint: str | None) -> str | None:
        """Rewrite node endpoints for containerized apishim reachability.

        In single-host dev flows, node agents may advertise hostnames that are not
        resolvable from the apishim container (or loopback addresses that resolve
        to the container itself). When AE_NODE_ADVERTISE_IP is set for apishim,
        use it as a fallback host in those cases.
        """
        if not endpoint:
            return endpoint
        fallback_host = (os.getenv("AE_NODE_ADVERTISE_IP") or "").strip()
        if not fallback_host:
            try:
                if Path("/.dockerenv").exists():
                    fallback_host = "host.containers.internal"
            except Exception:
                fallback_host = ""
        if not fallback_host:
            return endpoint
        try:
            parsed = urlparse(endpoint)
        except Exception:
            return endpoint
        host = str(parsed.hostname or "").strip()
        if not host:
            return endpoint
        host_l = host.lower()
        replace = host_l in {"127.0.0.1", "localhost", "::1"}
        if not replace:
            try:
                probe_port = parsed.port or (443 if (parsed.scheme or "http") == "https" else 80)
                socket.getaddrinfo(host, probe_port)
            except OSError:
                replace = True
        if not replace:
            return endpoint
        net_host = (
            f"[{fallback_host}]"
            if (":" in fallback_host and not fallback_host.startswith("["))
            else fallback_host
        )
        netloc = f"{net_host}:{parsed.port}" if parsed.port is not None else net_host
        try:
            normalized = parsed._replace(netloc=netloc).geturl()
        except Exception:
            return endpoint
        if normalized != endpoint:
            LOGGER.debug(
                "rewrote node endpoint for apishim runtime: %s -> %s", endpoint, normalized
            )
        return normalized

    def _runtime_for_endpoint(self, endpoint: str | None) -> RuntimeAdapter:
        endpoint = self._normalize_runtime_endpoint(endpoint)
        if not endpoint:
            return self.server.runtime  # type: ignore[attr-defined]
        agent_url = self._normalize_runtime_endpoint(  # type: ignore[attr-defined]
            getattr(self.server, "_agent_url", None)  # type: ignore[attr-defined]
        )
        if agent_url and endpoint == agent_url:
            return self.server.runtime  # type: ignore[attr-defined]
        cache = getattr(self.server, "_runtime_cache", None)  # type: ignore[attr-defined]
        if isinstance(cache, dict) and endpoint in cache:
            return cache[endpoint]
        try:
            from ae.runtime import RemoteRuntime

            base = getattr(self.server, "_runtime_base", self.server.runtime)  # type: ignore[attr-defined]
            runtime = RemoteRuntime(endpoint, base)
            if isinstance(cache, dict):
                cache[endpoint] = runtime
            return runtime
        except Exception:
            return self.server.runtime  # type: ignore[attr-defined]

    def _node_endpoint_for_id(self, node_id: str | None) -> str | None:
        if not node_id:
            return None
        state = getattr(self.server, "state", None)  # type: ignore[attr-defined]
        if state is None or not hasattr(state, "get_node"):
            return None
        try:
            rec = state.get_node(node_id)
        except Exception:
            return None
        if not rec:
            return None
        node, _status = rec
        return getattr(node, "endpoint", None)

    def _node_id_for_pod(
        self,
        pod_name: str,
        *,
        namespace: str | None = None,
        app_name: str | None = None,
        container_info: dict | None = None,
    ) -> str | None:
        labels = (container_info or {}).get("labels", {}) or {}
        node_id = labels.get("ae.node")
        if node_id:
            return str(node_id)
        state = getattr(self.server, "state", None)  # type: ignore[attr-defined]
        if state is None or not hasattr(state, "list_pod_nodes"):
            return None
        try:
            if app_name:
                rows = state.list_pod_nodes(app_name)
                for row in rows:
                    if row and row[0] == pod_name:
                        return str(row[1])
            if not hasattr(state, "list_status"):
                return None
            for status in state.list_status():
                if namespace and not str(getattr(status, "app_name", "")).startswith(
                    f"{namespace}/"
                ):
                    continue
                rows = state.list_pod_nodes(status.app_name)
                for row in rows:
                    if row and row[0] == pod_name:
                        return str(row[1])
        except Exception:
            return None
        return None

    def _runtime_for_pod(
        self, namespace: str | None, pod_name: str, *, container_info: dict | None = None
    ) -> tuple[RuntimeAdapter, str | None, str | None]:
        app_name = None
        if container_info:
            app_name = self._app_from_labels(container_info.get("labels", {}) or {})
        node_id = self._node_id_for_pod(
            pod_name, namespace=namespace, app_name=app_name, container_info=container_info
        )
        endpoint = self._node_endpoint_for_id(node_id)
        runtime = self._runtime_for_endpoint(endpoint)
        return runtime, node_id, endpoint

    def _node_record_for_ip(self, ip: str) -> Any | None:
        state = getattr(self.server, "state", None)  # type: ignore[attr-defined]
        if state is None or not hasattr(state, "list_nodes"):
            return None
        try:
            ip_obj = ipaddress.ip_address(ip)
        except Exception:
            return None
        try:
            nodes = state.list_nodes()
        except Exception:
            nodes = []
        for rec, _st in nodes:
            try:
                if rec.pod_cidr and ip_obj in ipaddress.ip_network(rec.pod_cidr, strict=False):
                    return rec
            except Exception:
                continue
        return None

    def _container_for_pod_ip(
        self, runtime: RuntimeAdapter, namespace: str | None, pod_ip: str
    ) -> dict | None:
        try:
            containers = runtime.list_containers_info()
        except Exception:
            return None
        for c in containers:
            if str(c.get("pod_ip") or "") != str(pod_ip):
                continue
            labels = c.get("labels", {}) or {}
            c_ns = labels.get("ae.namespace") or "default"
            if namespace and c_ns != namespace:
                continue
            return c
        return None

    def _resolve_pod_container(
        self, namespace: str | None, pod_name: str, *, runtime: RuntimeAdapter | None = None
    ) -> dict | None:
        target_runtime = runtime or self.server.runtime  # type: ignore[attr-defined]
        try:
            containers = target_runtime.list_containers_info()
        except Exception:
            containers = []
        for c in containers:
            labels = c.get("labels", {}) or {}
            rid = labels.get("ae.pod_name") or labels.get("ae.replica_id") or c.get("name")
            if rid != pod_name and c.get("name") != pod_name:
                continue
            c_ns = labels.get("ae.namespace") or "default"
            if namespace and c_ns != namespace:
                continue
            return c
        return None

    @staticmethod
    def _extract_pod_uid(qs: dict[str, list[str]] | None) -> str | None:
        if not qs:
            return None
        for key in qs:
            if key.lower() in {"uid", "poduid"}:
                vals = qs.get(key) or []
                if vals:
                    return str(vals[0])
        return None

    @staticmethod
    def _extract_pod_rv(qs: dict[str, list[str]] | None) -> int | None:
        if not qs:
            return None
        for key in qs:
            if key.lower() in {"resourceversion", "podrv", "rv"}:
                vals = qs.get(key) or []
                if not vals:
                    continue
                try:
                    return int(vals[0])
                except Exception:
                    return None
        return None

    @classmethod
    def _pod_watch_cache_key(cls, namespace: str | None, pod_name: str) -> tuple[str, str]:
        return (namespace or "default", pod_name)

    @classmethod
    def _update_pod_watch_cache(cls, pods: list[dict[str, Any]], default_rv: int) -> None:
        if not pods:
            return
        now = time.time()
        ttl = cls.pod_watch_ttl
        with cls.pod_watch_lock:
            for pod in pods:
                meta = pod.get("metadata", {}) or {}
                name = meta.get("name")
                if not name:
                    continue
                ns = meta.get("namespace") or "default"
                uid = meta.get("uid")
                rv_val = meta.get("resourceVersion", default_rv)
                try:
                    rv = int(rv_val) if rv_val is not None else int(default_rv)
                except Exception:
                    rv = int(default_rv)
                cls.pod_watch_cache[(ns, name)] = (uid, rv, now)
            if ttl and ttl > 0:
                cutoff = now - ttl
                for key, (_uid, _rv, seen) in list(cls.pod_watch_cache.items()):
                    if seen < cutoff:
                        cls.pod_watch_cache.pop(key, None)

    def _pod_watch_entry(
        self,
        namespace: str | None,
        pod_name: str,
    ) -> tuple[str | None, int, float] | None:
        key = self._pod_watch_cache_key(namespace, pod_name)
        now = time.time()
        with self.pod_watch_lock:
            entry = self.pod_watch_cache.get(key)
        if not entry:
            return None
        uid, _rv, seen = entry
        ttl = self.pod_watch_ttl
        if ttl and ttl > 0 and (now - seen) > ttl:
            return None
        return entry

    def _pod_watch_allows(
        self,
        namespace: str | None,
        pod_name: str,
        expected_uid: str | None = None,
        expected_rv: int | None = None,
    ) -> bool:
        entry = self._pod_watch_entry(namespace, pod_name)
        if not entry:
            return False
        uid, rv, _seen = entry
        if expected_uid:
            if not uid:
                return False
            if str(uid) != str(expected_uid):
                return False
        if expected_rv is not None and int(rv) != int(expected_rv):
            return False
        return True

    def _scope_allows(
        self,
        env_name: str,
        namespace: str | None,
        app: str | None,
        name: str,
        *,
        token_scopes: list[str] | None = None,
    ) -> bool:
        raw = os.getenv(env_name, "").strip()
        env_scopes = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
        principal_scopes = (
            token_scopes if token_scopes is not None else self._parse_principal().scopes
        )
        scopes: list[list[str]] = []
        if env_scopes:
            scopes.append(env_scopes)
        if principal_scopes:
            scopes.append([str(s) for s in principal_scopes if s])
        if not scopes:
            return True

        candidates: list[str] = []
        if namespace:
            if app:
                candidates.append(f"{namespace}/{app}")
            candidates.append(f"{namespace}/{name}")
        if app:
            candidates.append(app)
        candidates.append(name)

        for scope_list in scopes:
            matched = False
            for pat in scope_list:
                for cand in candidates:
                    if fnmatch.fnmatch(cand, pat):
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                return False
        return True

    def _validate_pod_scope(
        self,
        *,
        namespace: str | None,
        pod_name: str,
        scope_env: str,
        action: str,
        expected_uid: str | None = None,
        expected_rv: int | None = None,
    ) -> dict | None:
        runtime, _node_id, _endpoint = self._runtime_for_pod(namespace, pod_name)
        container = self._resolve_pod_container(namespace, pod_name, runtime=runtime)
        if container is None and runtime is not self.server.runtime:  # type: ignore[attr-defined]
            container = self._resolve_pod_container(
                namespace,
                pod_name,
                runtime=self.server.runtime,  # type: ignore[arg-type]
            )
        if not container:
            self._not_found()
            return None
        if expected_uid:
            actual_uid = container.get("uid") or container.get("id")
            if not actual_uid or str(actual_uid) != str(expected_uid):
                self._json_status(
                    HTTPStatus.CONFLICT,
                    reason="Conflict",
                    message="pod UID mismatch",
                )
                return None
        if not container.get("running", False):
            self._json_status(
                HTTPStatus.CONFLICT,
                reason="Conflict",
                message="pod is not running",
            )
            return None
        labels = container.get("labels", {}) or {}
        app = self._app_from_labels(labels) or pod_name
        if self.pod_state_check and hasattr(self.server, "state"):
            try:
                fn = getattr(self.server.state, "list_pod_nodes", None)  # type: ignore[attr-defined]
                if callable(fn):
                    found = False
                    for rid, _node, _ready, _live, _status, _rmsg, _lmsg in fn(app):
                        if str(rid) == str(pod_name):
                            found = True
                            break
                if not found:
                    self._json_status(
                        HTTPStatus.CONFLICT,
                        reason="Conflict",
                        message="pod not present in controller state",
                    )
                    return None
            except Exception:
                pass
        if expected_rv is not None:
            if not self._pod_watch_allows(namespace, pod_name, expected_uid, expected_rv):
                self._json_status(
                    HTTPStatus.CONFLICT,
                    reason="Conflict",
                    message="pod resourceVersion mismatch",
                )
                return None
        elif self.pod_watch_check and not self._pod_watch_allows(namespace, pod_name, expected_uid):
            self._json_status(
                HTTPStatus.CONFLICT,
                reason="Conflict",
                message="pod not present in watch cache",
            )
            return None
        if not self._scope_allows(
            scope_env,
            namespace,
            app,
            pod_name,
            token_scopes=self._parse_principal().scopes,
        ):
            self._deny(403, message=f"{action} scope denies target pod")
            return None
        return container

    def _service_app_name(self, svc: K8sObject) -> str:
        labels = svc.metadata.get("labels") or {}
        app = self._app_from_labels(labels)
        if app:
            return app
        selector = svc.spec.get("selector") or {}
        if isinstance(selector, dict):
            app = self._app_from_labels(selector)  # type: ignore[arg-type]
            if app:
                return app
        return svc.name

    def _validate_service_pf_scope(self, namespace: str | None, svc: K8sObject, name: str) -> bool:
        app = self._service_app_name(svc)
        if not self._scope_allows(
            "AE_API_PF_SCOPE",
            namespace,
            app,
            name,
            token_scopes=self._parse_principal().scopes,
        ):
            self._deny(403, message="port-forward scope denies target service")
            return False
        return True

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
        authed = principal.username != "system:unauthenticated"
        rbac_relaxed = self.rbac_enabled and self.rbac_eval_roles and authed
        ok = False
        if role == "write":
            ok = role_name == "admin"
        elif role == "read":
            ok = role_name in {"admin", "read"}
        elif role == "exec":
            ok = role_name in {"admin", "exec"}
        elif role == "portforward":
            ok = role_name in {"admin", "portforward"}
        elif role == "mint":
            ok = role_name in {"admin", "mint"}
        elif role in {"rbac-read", "rbac-write"}:
            ok = role_name in {"admin", "read"}
        if ok or (
            rbac_relaxed
            and role in {"read", "write", "exec", "portforward", "mint", "rbac-read", "rbac-write"}
        ):
            return True
        if self.allow_anonymous:
            return True
        self._json_status(
            HTTPStatus.UNAUTHORIZED,
            reason="Unauthorized",
            message="missing/invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _ha_mode_enabled(self) -> bool:
        return str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}

    def _ha_mutation_exempt(self, method: str, path: str) -> bool:
        if path == "/api/v1/sessiontokens":
            return True
        if path.startswith("/apis/authorization.k8s.io/"):
            return True
        if method == "POST" and re.match(r"^/api/v1/namespaces/[^/]+/pods/[^/]+/exec$", path):
            return True
        if method == "POST" and re.match(
            r"^/api/v1/namespaces/[^/]+/(pods|services)/[^/]+/portforward$", path
        ):
            return True
        return False

    def _reject_ha_workload_mutation(self, method: str, path: str) -> bool:
        if not self._ha_mode_enabled() or self._ha_mutation_exempt(method, path):
            return False
        supported = False
        controller_owned = False
        plural, _ns, _name = _ns_name(path)
        if plural in {
            "namespaces",
            "configmaps",
            "secrets",
            "serviceaccounts",
            "services",
            "persistentvolumeclaims",
            "persistentvolumes",
        }:
            supported = True
        d_plural, _d_ns, _d_name = _apps_ns_name(path)
        if d_plural in {"deployments", "deployments/scale", "statefulsets", "daemonsets"}:
            supported = True
        n_plural, _n_ns, _n_name = _net_ns_name(path)
        if n_plural == "ingresses":
            supported = True
        b_plural, _b_ns, _b_name = _batch_ns_name(path)
        if b_plural in {"jobs", "cronjobs"}:
            supported = True
        h_plural, _h_ns, _h_name = _gv_ns_name(
            path, "autoscaling", "v2", "horizontalpodautoscalers"
        )
        if h_plural == "horizontalpodautoscalers":
            supported = True
        crd_plural, _crd_name = _gv_cluster_name(
            path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
        )
        if crd_plural == "customresourcedefinitions":
            supported = True
        for resource in ("roles", "rolebindings"):
            r_plural, _r_ns, _r_name = _gv_ns_name(
                path, "rbac.authorization.k8s.io", "v1", resource
            )
            if r_plural == resource:
                supported = True
        for resource in ("clusterroles", "clusterrolebindings"):
            r_plural, _r_name = _gv_cluster_name(
                path, "rbac.authorization.k8s.io", "v1", resource
            )
            if r_plural == resource:
                supported = True
        p_plural, _p_ns, _p_name = _gv_ns_name(path, "policy", "v1", "poddisruptionbudgets")
        if p_plural == "poddisruptionbudgets":
            supported = True
        s_plural, _s_name = _gv_cluster_name(path, "storage.k8s.io", "v1", "storageclasses")
        if s_plural == "storageclasses":
            supported = True
        for resource in ("csidrivers", "csinodes"):
            c_plural, _c_name = _gv_cluster_name(path, "storage.k8s.io", "v1", resource)
            if c_plural == resource:
                supported = True
        for resource in ("volumeattachments",):
            c_plural, _c_name = _gv_cluster_name(path, "storage.k8s.io", "v1", resource)
            if c_plural == resource and is_controller_owned_storage_authority_resource(
                "storage.k8s.io", "v1", resource
            ):
                controller_owned = True
        for resource in ("csistoragecapacities",):
            c_plural, _c_ns, _c_name = _gv_ns_name(path, "storage.k8s.io", "v1", resource)
            if c_plural == resource and is_controller_owned_storage_authority_resource(
                "storage.k8s.io", "v1", resource
            ):
                controller_owned = True
        for resource in ("volumesnapshotclasses",):
            snap_plural, _snap_name = _gv_cluster_name(
                path, "snapshot.storage.k8s.io", "v1", resource
            )
            if snap_plural == resource:
                supported = True
        for resource in ("volumesnapshotcontents",):
            snap_plural, _snap_name = _gv_cluster_name(
                path, "snapshot.storage.k8s.io", "v1", resource
            )
            if snap_plural == resource and is_controller_owned_storage_authority_resource(
                "snapshot.storage.k8s.io", "v1", resource
            ):
                controller_owned = True
        for resource in ("volumesnapshots",):
            snap_plural, _snap_ns, _snap_name = _gv_ns_name(
                path, "snapshot.storage.k8s.io", "v1", resource
            )
            if snap_plural == resource:
                supported = True
        self._refresh_crd_registry_from_state()
        custom = _parse_custom_resource_path(path)
        if custom is not None:
            group, version, _namespace, plural, _name = custom
            if self._lookup_crd(group, version, plural):
                supported = True
        if controller_owned:
            self._json_status(
                HTTPStatus.CONFLICT,
                reason="HAUnsupported",
                message=(
                    "HA mutation via apishim is read-only for controller-owned storage "
                    "resources; this resource is managed by the elected storage controller"
                ),
            )
            return True
        if supported:
            return False
        self._json_status(
            HTTPStatus.CONFLICT,
            reason="HAUnsupported",
            message="HA mutation via apishim is enabled only for converged H4 resources; this resource remains read-only until its later H4* slice",
        )
        return True

    def _audit(self, action: str, **fields: Any) -> None:
        try:
            principal = self._parse_principal()
            parts = []
            for key, val in fields.items():
                if val is None or val == "":
                    continue
                parts.append(f"{key}={val}")
            suffix = " ".join(parts)
            LOGGER.info(
                "audit %s user=%s role=%s %s",
                action,
                principal.username,
                principal.token_role or "-",
                suffix,
            )
        except Exception:
            return

    def _eval_subject_access_review(self, spec: dict[str, Any]) -> dict[str, Any]:
        res_attr = (spec or {}).get("resourceAttributes") or {}
        verb = (res_attr.get("verb") or "").lower()
        resource = res_attr.get("resource") or ""
        subres = res_attr.get("subresource")
        namespace = res_attr.get("namespace")
        if subres:
            resource = f"{resource}/{subres}"
        if not verb or not resource:
            non_attr = (spec or {}).get("nonResourceAttributes") or {}
            nverb = (non_attr.get("verb") or "").lower()
            path = non_attr.get("path") or ""
            if not nverb or not path:
                return {"allowed": False, "denied": True, "reason": "missing verb/resource"}
            principal = self._parse_principal()
            if self.rbac_enabled and self.rbac_eval_roles:
                allowed = principal.token_role in {"admin", "read"}
            else:
                allowed = principal.username != "system:unauthenticated"
            return {
                "allowed": allowed,
                "denied": not allowed,
                "reason": "rbac: allowed" if allowed else "rbac: forbidden",
            }
        allowed = self._rbac_allows(verb, resource, namespace)
        return {
            "allowed": allowed,
            "denied": not allowed,
            "reason": "rbac: allowed" if allowed else "rbac: forbidden",
        }

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
        if role == "admin":
            return True
        if role == "read" and verb in {"get", "list", "watch"}:
            return True
        if not self.rbac_eval_roles and role is None:
            return False
        if self.rbac_eval_roles and principal.username == "system:unauthenticated":
            return False
        # Static policy fallback
        if not self.rbac_eval_roles:
            allowed = self.rbac_policies.get((verb, resource)) or self.rbac_policies.get(
                (verb, "*")
            )
            return bool(allowed and role in allowed)
        # Role/RoleBinding evaluation
        user = principal.username
        groups = principal.groups
        sa_ns = None
        sa_name = None
        if user.startswith("system:serviceaccount:"):
            parts = user.split(":")
            if len(parts) >= 4:
                sa_ns = parts[2]
                sa_name = parts[3]
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
                    or (
                        s.get("kind") == "ServiceAccount"
                        and sa_name
                        and s.get("name") == sa_name
                        and (s.get("namespace") or rb.namespace) == sa_ns
                    )
                    for s in subjects
                ):
                    continue
                ref = (rb.spec or {}).get("roleRef", {})
                rname = ref.get("name")
                if not rname:
                    continue
                role_obj = self.store.get(
                    "rbac.authorization.k8s.io", "v1", "roles", rb.namespace, rname
                )  # type: ignore[attr-defined]
                if role_obj:
                    for rule in (role_obj.spec or {}).get("rules", []):
                        if _rule_matches(resource, rule.get("resources", [])):
                            allowed_verbs.update(rule.get("verbs", []))
            # ClusterRoleBindings
            for crb in self.store.list_all(
                "rbac.authorization.k8s.io", "v1", "clusterrolebindings"
            ):  # type: ignore[attr-defined]
                subjects = (crb.spec or {}).get("subjects", [])
                if not any(
                    (s.get("kind") == "User" and s.get("name") == user)
                    or (s.get("kind") == "Group" and s.get("name") in groups)
                    or (
                        s.get("kind") == "ServiceAccount"
                        and sa_name
                        and s.get("name") == sa_name
                        and (s.get("namespace") or "") == (sa_ns or "")
                    )
                    for s in subjects
                ):
                    continue
                ref = (crb.spec or {}).get("roleRef", {})
                rname = ref.get("name")
                if not rname:
                    continue
                crobj = self.store.get(
                    "rbac.authorization.k8s.io", "v1", "clusterroles", None, rname
                )  # type: ignore[attr-defined]
                if crobj:
                    for rule in (crobj.spec or {}).get("rules", []):
                        if _rule_matches(resource, rule.get("resources", [])):
                            allowed_verbs.update(rule.get("verbs", []))
        except Exception:
            return False
        if not allowed_verbs:
            # fallback to static if no rules matched
            allowed = self.rbac_policies.get((verb, resource)) or self.rbac_policies.get(
                (verb, "*")
            )
            return bool(allowed and role in allowed)
        return verb in allowed_verbs

    def _ok(self, payload: dict[str, Any]) -> None:
        data = _json(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self._emit_warnings()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self, msg: str = "not found") -> None:
        self._json_status(HTTPStatus.NOT_FOUND, reason="NotFound", message=msg)

    def _json_status(
        self,
        code: int,
        *,
        reason: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
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
        if headers:
            for key, value in headers.items():
                self.send_header(str(key), str(value))
        self._emit_warnings()
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
                line = line.strip()
                if b";" in line:
                    line = line.split(b";", 1)[0]
                try:
                    chunk_len = int(line, 16)
                except Exception:
                    break
                if chunk_len == 0:
                    # consume trailers until blank line
                    while True:
                        trailer = self.rfile.readline()
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                body.extend(self.rfile.read(chunk_len))
                # consume chunk trailer CRLF
                self.rfile.read(2)
            return bytes(body)
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length)

    def _deny(self, code: int = HTTPStatus.FORBIDDEN, message: str = "forbidden") -> None:
        self._json_status(
            int(code), reason="Forbidden" if int(code) == 403 else "Unauthorized", message=message
        )

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
    def _handle_port_forward_ws(
        self,
        target_host: str,
        target_port: int,
        *,
        upstream_factory=None,
    ) -> None:
        """Minimal WebSocket port-forward bridge (single connection, multi-port)."""

        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return
        accept_seed = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("utf-8")
        accept = base64.b64encode(hashlib.sha1(accept_seed).digest()).decode("utf-8")  # noqa: S324 - RFC 6455 requires SHA-1
        subproto_hdr = self.headers.get("Sec-WebSocket-Protocol")
        chosen_proto = None
        if subproto_hdr:
            chosen_proto = subproto_hdr.split(",")[0].strip()
        if PF_DEBUG:
            LOGGER.warning(
                "portforward ws handshake proto=%s target=%s:%s",
                chosen_proto,
                target_host,
                target_port,
            )

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
                length = len(payload)
                if length < 126:
                    header.append(length)
                elif length < (1 << 16):
                    header.append(126)
                    header.extend(length.to_bytes(2, "big"))
                else:
                    header.append(127)
                    header.extend(length.to_bytes(8, "big"))
                sock.sendall(header + payload)
            except Exception as exc:
                if PF_DEBUG:
                    LOGGER.warning("portforward ws send failed: %s", exc)
                pass

        # Handshake
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if chosen_proto:
            self.send_header("Sec-WebSocket-Protocol", chosen_proto)
        self.end_headers()
        max_seconds, idle_seconds = self._stream_limits()
        max_bytes = self._stream_byte_limit()
        bytes_in = 0
        bytes_out = 0
        start_ts = time.time()
        last_activity = start_ts
        use_timeouts = bool(max_seconds or idle_seconds)
        if use_timeouts:
            try:
                self.connection.settimeout(0.1)
            except Exception:
                pass

        def _expired(now: float | None = None) -> bool:
            if not max_seconds and not idle_seconds:
                return False
            check = now or time.time()
            if max_seconds and (check - start_ts) > max_seconds:
                return True
            if idle_seconds and (check - last_activity) > idle_seconds:
                return True
            return False

        # Native WebSocket port-forward protocol (portforward.k8s.io)
        if chosen_proto and "portforward.k8s.io" in chosen_proto:
            channel_data = 0  # data channel for first port
            channel_error = 1
            upstream = None
            try:
                if upstream_factory:
                    upstream = upstream_factory(target_port)
                if not upstream:
                    upstream = socket.create_connection((target_host, target_port), timeout=5.0)
                if upstream:
                    upstream.settimeout(0.1)
            except Exception:
                if PF_DEBUG:
                    LOGGER.warning(
                        "portforward ws upstream connect failed target=%s:%s",
                        target_host,
                        target_port,
                    )
                upstream = None

            stop = False
            recv_from_up = 0
            send_to_up = 0

            def _pump_upstream() -> None:
                nonlocal stop
                nonlocal recv_from_up
                nonlocal last_activity
                nonlocal bytes_out
                if not upstream:
                    return
                while not stop:
                    if _expired():
                        stop = True
                        break
                    try:
                        chunk = upstream.recv(4096)
                        if not chunk:
                            break
                        last_activity = time.time()
                        bytes_out += len(chunk)
                        if max_bytes and (bytes_in + bytes_out) > max_bytes:
                            stop = True
                            break
                        recv_from_up += len(chunk)
                        frame = bytes([channel_data]) + chunk
                        _send_ws(self.connection, frame, opcode=0x2)
                        if PF_DEBUG and (recv_from_up < 8192 or recv_from_up % 65536 == 0):
                            LOGGER.warning(
                                "portforward ws upstream->client bytes=%s",
                                recv_from_up,
                            )
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                stop = True

            t_up = threading.Thread(target=_pump_upstream, daemon=True)
            t_up.start()

            while not stop:
                if _expired():
                    break
                msg = _recv_ws(self.connection)
                if msg is None:
                    if use_timeouts and not _expired():
                        continue
                    break
                opcode, payload = msg
                if opcode == 0x8:
                    break
                if opcode not in (0x1, 0x2) or not payload:
                    continue
                bytes_in += len(payload)
                if max_bytes and (bytes_in + bytes_out) > max_bytes:
                    break
                ch = payload[0]
                data = payload[1:]
                if ch == channel_error:
                    # ignore client-to-error channel payloads
                    continue
                if upstream and data:
                    try:
                        last_activity = time.time()
                        upstream.sendall(data)
                        send_to_up += len(data)
                        if PF_DEBUG and (send_to_up < 8192 or send_to_up % 65536 == 0):
                            LOGGER.warning(
                                "portforward ws client->upstream bytes=%s",
                                send_to_up,
                            )
                    except Exception:
                        break
            stop = True
            if upstream:
                try:
                    upstream.close()
                except Exception:
                    pass
            try:
                self.connection.close()
            except Exception:
                pass
            return

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
                self._handle_port_forward_spdy(
                    target_host,
                    [target_port],
                    conn_override=ws_conn,
                    suppress_handshake=True,
                    upstream_factory=upstream_factory,
                )
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
                if upstream_factory:
                    s = upstream_factory(port)
                if not s:
                    s = socket.create_connection((target_host, port), timeout=5.0)
                s.settimeout(0.1)
                upstream_socks[port] = s
                return s
            except Exception:
                return None

        def _pump_from_client() -> None:
            nonlocal stop
            nonlocal last_activity
            nonlocal bytes_in
            while not stop:
                if _expired():
                    stop = True
                    break
                msg = _recv_ws(self.connection)
                if msg is None:
                    if use_timeouts and not _expired():
                        continue
                    break
                opcode, payload = msg
                if opcode == 0x8:  # close
                    stop = True
                    break
                if opcode not in (0x1, 0x2) or len(payload) < 2:
                    continue
                bytes_in += len(payload)
                if max_bytes and (bytes_in + bytes_out) > max_bytes:
                    stop = True
                    break
                try:
                    with open("/tmp/pf-debug.log", "ab") as dbg:  # noqa: S108
                        dbg.write(payload + b"\n")
                except Exception:
                    pass
                port = int.from_bytes(payload[:2], "big")
                data = payload[2:]
                sock = _get_upstream(port or target_port)
                if sock and data:
                    try:
                        last_activity = time.time()
                        sock.sendall(data)
                    except Exception:
                        stop = True
                        break

        def _pump_to_client(port: int, sock: socket.socket) -> None:
            nonlocal stop
            nonlocal last_activity
            nonlocal bytes_out
            while not stop:
                if _expired():
                    stop = True
                    break
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    last_activity = time.time()
                    bytes_out += len(chunk)
                    if max_bytes and (bytes_in + bytes_out) > max_bytes:
                        stop = True
                        break
                    frame = port.to_bytes(2, "big") + chunk
                    _send_ws(self.connection, frame, opcode=0x2)
                except TimeoutError:
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
            t = threading.Thread(
                target=_pump_to_client, args=(target_port, first_sock), daemon=True
            )
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
    def _handle_port_forward_spdy(
        self,
        target_host: str,
        target_ports: list[int],
        target_hosts_by_port: dict[int, str] | None = None,
        port_map: dict[int, int] | None = None,
        *,
        conn_override=None,
        suppress_handshake: bool = False,
        upstream_factory=None,
    ) -> None:
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
            # Negotiate portforward stream protocol explicitly for kubectl
            stream_proto = self.headers.get("X-Stream-Protocol-Version") or "portforward.k8s.io"
            # If client sent a CSV list, pick the first entry
            if "," in stream_proto:
                stream_proto = stream_proto.split(",")[0].strip()
            self.send_header("X-Stream-Protocol-Version", stream_proto)
            self.end_headers()
        try:
            conn.settimeout(0.05)
        except Exception:
            pass

        if not target_ports:
            target_ports = [0]

        def _pf_debug(msg: str) -> None:
            try:
                with open("/tmp/pf-debug.txt", "a") as f:  # noqa: S108
                    f.write(msg + "\n")
            except Exception:
                pass

        _pf_debug("spdy-start")
        max_seconds, idle_seconds = self._stream_limits()
        max_bytes = self._stream_byte_limit()
        bytes_in = 0
        bytes_out = 0
        start_ts = time.time()
        last_activity = start_ts

        def _expired(now: float | None = None) -> bool:
            if not max_seconds and not idle_seconds:
                return False
            check = now or time.time()
            if max_seconds and (check - start_ts) > max_seconds:
                return True
            if idle_seconds and (check - last_activity) > idle_seconds:
                return True
            return False

        SPDY_DICT = base64.b64decode(
            "AAAAB29wdGlvbnMAAAAEaGVhZAAAAARwb3N0AAAAA3B1dAAAAAZkZWxldGUAAAAFdHJhY2UAAAAGYWNjZXB0AAAADmFjY2VwdC1jaGFyc2V0AAAAD2FjY2VwdC1lbmNvZGluZwAAAA9hY2NlcHQtbGFuZ3VhZ2UAAAANYWNjZXB0LXJhbmdlcwAAAANhZ2UAAAAFYWxsb3cAAAANYXV0aG9yaXphdGlvbgAAAA1jYWNoZS1jb250cm9sAAAACmNvbm5lY3Rpb24AAAAMY29udGVudC1iYXNlAAAAEGNvbnRlbnQtZW5jb2RpbmcAAAAQY29udGVudC1sYW5ndWFnZQAAAA5jb250ZW50LWxlbmd0aAAAABBjb250ZW50LWxvY2F0aW9uAAAAC2NvbnRlbnQtbWQ1AAAADWNvbnRlbnQtcmFuZ2UAAAAMY29udGVudC10eXBlAAAABGRhdGUAAAAEZXRhZwAAAAZleHBlY3QAAAAHZXhwaXJlcwAAAARmcm9tAAAABGhvc3QAAAAIaWYtbWF0Y2gAAAARaWYtbW9kaWZpZWQtc2luY2UAAAANaWYtbm9uZS1tYXRjaAAAAAhpZi1yYW5nZQAAABNpZi11bm1vZGlmaWVkLXNpbmNlAAAADWxhc3QtbW9kaWZpZWQAAAAIbG9jYXRpb24AAAAMbWF4LWZvcndhcmRzAAAABnByYWdtYQAAABJwcm94eS1hdXRoZW50aWNhdGUAAAATcHJveHktYXV0aG9yaXphdGlvbgAAAAVyYW5nZQAAAAdyZWZlcmVyAAAAC3JldHJ5LWFmdGVyAAAABnNlcnZlcgAAAAJ0ZQAAAAd0cmFpbGVyAAAAEXRyYW5zZmVyLWVuY29kaW5nAAAAB3VwZ3JhZGUAAAAKdXNlci1hZ2VudAAAAAR2YXJ5AAAAA3ZpYQAAAAd3YXJuaW5nAAAAEHd3dy1hdXRoZW50aWNhdGUAAAAGbWV0aG9kAAAAA2dldAAAAAZzdGF0dXMAAAAGMjAwIE9LAAAAB3ZlcnNpb24AAAAISFRUUC8xLjEAAAADdXJsAAAABnB1YmxpYwAAAApzZXQtY29va2llAAAACmtlZXAtYWxpdmUAAAAGb3JpZ2luMTAwMTAxMjAxMjAyMjA1MjA2MzAwMzAyMzAzMzA0MzA1MzA2MzA3NDAyNDA1NDA2NDA3NDA4NDA5NDEwNDExNDEyNDEzNDE0NDE1NDE2NDE3NTAyNTA0NTA1MjAzIE5vbi1BdXRob3JpdGF0aXZlIEluZm9ybWF0aW9uMjA0IE5vIENvbnRlbnQzMDEgTW92ZWQgUGVybWFuZW50bHk0MDAgQmFkIFJlcXVlc3Q0MDEgVW5hdXRob3JpemVkNDAzIEZvcmJpZGRlbjQwNCBOb3QgRm91bmQ1MDAgSW50ZXJuYWwgU2VydmVyIEVycm9yNTAxIE5vdCBJbXBsZW1lbnRlZDUwMyBTZXJ2aWNlIFVuYXZhaWxhYmxlSmFuIEZlYiBNYXIgQXByIE1heSBKdW4gSnVsIEF1ZyBTZXB0IE9jdCBOb3YgRGVjIDAwOjAwOjAwIE1vbiwgVHVlLCBXZWQsIFRodSwgRnJpLCBTYXQsIFN1biwgR01UY2h1bmtlZCx0ZXh0L2h0bWwsaW1hZ2UvcG5nLGltYWdlL2pwZyxpbWFnZS9naWYsYXBwbGljYXRpb24veG1sLGFwcGxpY2F0aW9uL3hodG1sK3htbCx0ZXh0L3BsYWluLHRleHQvamF2YXNjcmlwdCxwdWJsaWNwcml2YXRlbWF4LWFnZT1nemlwLGRlZmxhdGUsc2RjaGNoYXJzZXQ9dXRmLThjaGFyc2V0PWlzby04ODU5LTEsdXRmLSwqLGVucT0wLg=="
        )
        dctx = zlib.decompressobj(wbits=15, zdict=SPDY_DICT)
        cctx = zlib.compressobj(wbits=15, zdict=SPDY_DICT)

        window_size = 1 << 20  # 1MiB default
        stream_windows: dict[int, int] = {}
        data_streams: dict[int, int] = {}  # stream_id -> target_port
        error_streams: dict[int, int] = {}  # stream_id -> data_stream sid
        upstream_cache: dict[int, socket.socket] = {}
        host_by_port = target_hosts_by_port or {}
        pf_port_map = port_map or {}
        _pf_debug(
            "handshake upgrade="
            + str(self.headers.get("Upgrade"))
            + " stream-proto="
            + str(self.headers.get("X-Stream-Protocol-Version"))
        )
        _pf_debug(
            f"targets host={target_host} ports={target_ports} host_by_port={host_by_port} port_map={pf_port_map}"
        )
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
                payload.append(0)  # flags (persist value = 0)
                payload += sid.to_bytes(3, "big")  # 24-bit ID per SPDY/3
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
            """Parse SPDY/3.1 header block using the official dictionary."""
            headers: dict[str, str] = {}
            _pf_debug(f"syn-len {len(payload)}")
            if len(payload) < 10:
                return headers
            header_block = payload[10:]
            try:
                decompressed = dctx.decompress(header_block)
            except Exception as e:
                _pf_debug(f"syn-parse-fail len={len(payload)} err={e}")
                return headers
            try:
                import io

                f = io.BytesIO(decompressed)
                num = int.from_bytes(f.read(4), "big")
                for _ in range(num):
                    nlen = int.from_bytes(f.read(4), "big")
                    name = f.read(nlen).decode("utf-8", "ignore")
                    vlen = int.from_bytes(f.read(4), "big")
                    value = f.read(vlen).decode("utf-8", "ignore")
                    headers[name] = value
                _pf_debug(f"syn-headers {headers}")
            except Exception as e:
                _pf_debug(f"syn-parse-fail len={len(payload)} err={e}")
                return headers
            return headers

        def send_syn_reply(stream_id: int) -> None:
            nv = [(":status", "200"), (":version", "HTTP/1.1")]
            buf = bytearray()
            buf += len(nv).to_bytes(4, "big")
            for name, value in nv:
                nb = name.encode("utf-8")
                vb = value.encode("utf-8")
                buf += len(nb).to_bytes(4, "big") + nb
                buf += len(vb).to_bytes(4, "big") + vb
            compressed = cctx.compress(bytes(buf)) + cctx.flush(zlib.Z_SYNC_FLUSH)
            payload = bytearray()
            payload += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            payload += compressed
            header = bytearray()
            header += b"\x80\x03"  # control + version 3
            header += (0x02).to_bytes(2, "big")  # SYN_REPLY
            header.append(0)  # flags
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + bytes(payload))

        try:
            send_settings({0x04: window_size})  # advertise window
            last_ping = time.time()
            while True:
                now = time.time()
                if _expired(now):
                    break
                if max_bytes and (bytes_in + bytes_out) > max_bytes:
                    break
                if now - last_ping > 10:
                    try:
                        send_ping()
                    except Exception:
                        break
                    last_ping = now

                try:
                    hdr = read_exact(conn, 8)
                except TimeoutError:
                    hdr = None
                if hdr:
                    last_activity = time.time()
                    if len(hdr) < 8:
                        _pf_debug("recv-short")
                        break
                    is_control = (hdr[0] & 0x80) != 0
                    if is_control:
                        frame_type = int.from_bytes(hdr[2:4], "big")
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        _pf_debug(f"ctrl frame type={frame_type} flags={flags} len={length}")
                        if length > (1 << 20):
                            send_goaway(status=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if frame_type == 1:  # SYN_STREAM
                            sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                            _pf_debug(f"syn-raw len={len(payload)} data={payload[:40].hex()}")
                            headers = parse_syn_stream(payload)
                            stype = headers.get("streamtype", "").lower()
                            if not stype:
                                stype = "data" if not data_streams else "error"
                            try:
                                port = int(
                                    headers.get("port")
                                    or headers.get("streamname")
                                    or target_ports[0]
                                )
                            except Exception:
                                port = target_ports[0]
                            _pf_debug(
                                f"SYN_STREAM sid={sid} stype={stype} port={port} targets={target_ports}"
                            )
                            if (
                                port not in target_ports
                                and port not in pf_port_map
                                and target_ports[0] != 0
                                and not isinstance(self.server.runtime, StubRuntime)
                            ):  # type: ignore[attr-defined]
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
                            try:
                                send_syn_reply(sid)
                            except Exception:
                                pass
                        elif frame_type == 4:  # SETTINGS
                            try:
                                num = int.from_bytes(payload[0:4], "big")
                                idx = 4
                                for _ in range(num):
                                    if idx + 8 > len(payload):
                                        break
                                    _flags = payload[idx]
                                    sid_setting = int.from_bytes(payload[idx + 1 : idx + 3], "big")
                                    val = int.from_bytes(payload[idx + 3 : idx + 7], "big")
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
                            _pf_debug("pong")
                            send_ping(payload[:4])
                        elif frame_type == 7:  # GOAWAY
                            _pf_debug("goaway")
                            break
                        if flags & 0x01:  # FIN on control frame
                            continue
                    else:
                        stream_id = int.from_bytes(hdr[0:4], "big") & 0x7FFFFFFF
                        flags = hdr[4]
                        length = int.from_bytes(hdr[5:8], "big")
                        _pf_debug(f"data frame sid={stream_id} flags={flags} len={length}")
                        if length > (1 << 20):
                            send_rst(stream_id, code=2)
                            break
                        payload = read_exact(conn, length) or b""
                        if payload:
                            last_activity = time.time()
                            bytes_in += len(payload)
                        if stream_id in data_streams and payload:
                            port = data_streams[stream_id]
                            wnd = stream_windows.get(stream_id, window_size)
                            if wnd <= 0:
                                send_rst(stream_id, code=2)
                                continue
                            if stream_id not in upstream_cache:
                                try:
                                    dest_host = host_by_port.get(port, target_host)
                                    dest_port = pf_port_map.get(port, port)
                                    if isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                                        try:
                                            dest_port = int(
                                                os.getenv("AE_STUB_BACKEND_PORT", "8081")
                                            )
                                        except Exception:
                                            dest_port = port
                                        dest_host = os.getenv("AE_STUB_BACKEND_HOST", dest_host)
                                    _pf_debug(
                                        f"connect sid={stream_id} host={dest_host} port={dest_port}"
                                    )
                                    if upstream_factory:
                                        upstream_cache[stream_id] = upstream_factory(dest_port)
                                    if not upstream_cache.get(stream_id):
                                        upstream_cache[stream_id] = socket.create_connection(
                                            (dest_host, dest_port), timeout=5.0
                                        )
                                    if upstream_cache.get(stream_id):
                                        upstream_cache[stream_id].settimeout(0.05)
                                    else:
                                        upstream_cache.pop(stream_id, None)
                                        continue
                                except Exception as e:
                                    _pf_debug(f"connect-fail sid={stream_id} port={port} err={e}")
                                    upstream_cache.pop(stream_id, None)
                                    continue
                            try:
                                last_activity = time.time()
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
                            last_activity = time.time()
                            bytes_out += len(resp)
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
        except Exception as exc:  # pragma: no cover - diagnostic
            _pf_debug(f"spdy-error {exc}")
        finally:
            _pf_debug("spdy-end")
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

    # ---------------- WebSocket exec/attach (browser + WS clients) ----------------
    def _handle_exec_ws(
        self,
        *,
        namespace: str | None,
        pod_name: str,
        command: list[str],
        container: str | None,
        tty: bool,
        want_stdin: bool,
        want_stdout: bool,
        want_stderr: bool,
        runtime: RuntimeAdapter | None = None,
    ) -> None:
        stream_debug = SPDY_DEBUG or str(
            os.getenv("AE_APISHIM_SPDY_DEBUG", "")
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if stream_debug:
            msg = (
                f"WS exec start pod={pod_name} container={container} tty={tty} "
                f"cmd={' '.join(command)}"
            )
            LOGGER.warning(msg)
            print(msg, file=sys.stderr, flush=True)
            _spdy_debug_line(msg)
        self._audit(
            "exec.start",
            namespace=namespace,
            pod=pod_name,
            container=container or "",
            tty=tty,
            stdin=want_stdin,
            stdout=want_stdout,
            stderr=want_stderr,
            cmd=" ".join(command),
        )
        rt = runtime or self.server.runtime  # type: ignore[attr-defined]
        # Try to open an attached exec session on the runtime (docker/podman only for now)
        exec_sock = None
        exec_id = None
        if hasattr(rt, "exec_attach"):
            try:
                exec_sock, exec_id = rt.exec_attach(  # type: ignore[attr-defined]
                    pod_name, command, container=container, tty=tty
                )
                exec_sock.settimeout(0.05)
            except Exception as exc:
                if SPDY_DEBUG:
                    LOGGER.warning("WS exec_attach failed: %s", exc)
                exec_sock = None
        if exec_sock is None:
            self._json_status(
                HTTPStatus.NOT_IMPLEMENTED,
                reason="NotImplemented",
                message="Streaming exec not available for this runtime",
            )
            return

        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            return
        accept_seed = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("utf-8")
        accept = base64.b64encode(hashlib.sha1(accept_seed).digest()).decode("utf-8")  # noqa: S324 - RFC 6455 requires SHA-1
        subproto_hdr = self.headers.get("Sec-WebSocket-Protocol")
        supported = [
            "v5.channel.k8s.io",
            "v4.channel.k8s.io",
            "v3.channel.k8s.io",
            "v2.channel.k8s.io",
            "channel.k8s.io",
        ]
        chosen_proto = None
        if subproto_hdr:
            requested = [p.strip() for p in subproto_hdr.split(",") if p.strip()]
            for proto in requested:
                if proto in supported:
                    chosen_proto = proto
                    break
        if chosen_proto is None:
            chosen_proto = supported[0]

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
                length = len(payload)
                if length < 126:
                    header.append(length)
                elif length < (1 << 16):
                    header.append(126)
                    header.extend(length.to_bytes(2, "big"))
                else:
                    header.append(127)
                    header.extend(length.to_bytes(8, "big"))
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
        if stream_debug:
            msg = f"WS exec stream-proto={chosen_proto}"
            LOGGER.warning(msg)
            print(msg, file=sys.stderr, flush=True)
            _spdy_debug_line(msg)

        conn = self.connection
        conn.settimeout(0.05)

        def _send_channel(ch: int, data: bytes) -> None:
            if not data:
                return
            _send_ws(conn, bytes([ch]) + data, opcode=0x2)

        def demux_exec_frame(frame: bytes) -> tuple[int, bytes] | None:
            # Docker multiplexed attach header: 1 byte stream, 3 bytes zero, 4 bytes length
            if len(frame) < 8:
                return None
            stream_type = frame[0]
            size = int.from_bytes(frame[4:8], "big")
            if size == 0:
                return None
            data = frame[8 : 8 + size]
            if len(data) < size:
                return None
            return stream_type, data

        exec_done = False
        exec_done_at = 0.0
        exec_grace_seconds = 2.0
        exec_buf = b""
        stop_reason = "unknown"
        max_seconds, idle_seconds = self._stream_limits()
        max_bytes = self._stream_byte_limit()
        bytes_in = 0
        bytes_out = 0
        start_ts = time.time()
        last_activity = start_ts
        try:
            while True:
                now = time.time()
                if max_seconds and (now - start_ts) > max_seconds:
                    stop_reason = "max_seconds"
                    break
                if idle_seconds and (now - last_activity) > idle_seconds:
                    stop_reason = "idle_timeout"
                    break
                if max_bytes and (bytes_in + bytes_out) > max_bytes:
                    stop_reason = "byte_limit"
                    break
                # Read from WebSocket client
                try:
                    msg = _recv_ws(conn)
                except TimeoutError:
                    msg = None
                except Exception:
                    msg = None
                if msg:
                    last_activity = time.time()
                    opcode, payload = msg
                    if opcode == 0x8:  # close
                        stop_reason = "client_close"
                        break
                    if opcode in (0x1, 0x2) and payload:
                        bytes_in += len(payload)
                        ch = payload[0]
                        data = payload[1:]
                        if ch == 0 and want_stdin and data:
                            try:
                                exec_sock.sendall(data)
                            except Exception:
                                pass
                        elif ch == 4 and tty and data:
                            try:
                                doc = json.loads(data.decode("utf-8", "ignore"))
                                h = (
                                    int(doc.get("Height"))
                                    if doc.get("Height") is not None
                                    else None
                                )
                                w = int(doc.get("Width")) if doc.get("Width") is not None else None
                                if hasattr(rt, "exec_resize"):
                                    try:
                                        rt.exec_resize(  # type: ignore[attr-defined]
                                            exec_id or "", height=h, width=w
                                        )
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                # Read from exec socket
                if exec_sock:
                    try:
                        chunk = exec_sock.recv(4096)
                    except TimeoutError:
                        chunk = None
                    except Exception:
                        chunk = b""
                    if chunk:
                        last_activity = time.time()
                        bytes_out += len(chunk)
                        if tty:
                            if want_stdout:
                                _send_channel(1, chunk)
                        else:
                            exec_buf += chunk
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
                                        _send_channel(1, data)
                                    elif stype == 2 and want_stderr:
                                        _send_channel(2, data)
                    elif chunk == b"":
                        exec_done = True
                        exec_done_at = time.time()
                        try:
                            exec_sock.close()
                        except Exception:
                            pass
                        exec_sock = None

                if exec_done and (time.time() - exec_done_at) > exec_grace_seconds:
                    stop_reason = "exec_done"
                    break
        finally:
            exit_code = 0
            try:
                if exec_id and hasattr(rt, "exec_exit_code"):
                    exit_code = int(rt.exec_exit_code(exec_id))  # type: ignore[attr-defined]
            except Exception:
                exit_code = 0
            if stream_debug:
                msg = (
                    "WS exec end "
                    f"pod={pod_name} container={container} exit_code={exit_code} "
                    f"bytes_in={bytes_in} bytes_out={bytes_out} reason={stop_reason}"
                )
                LOGGER.warning(msg)
                print(msg, file=sys.stderr, flush=True)
                _spdy_debug_line(msg)
            status_obj = _exec_status_obj(exit_code)
            try:
                _send_channel(3, json.dumps(status_obj, separators=(",", ":")).encode("utf-8"))
            except Exception:
                pass
            try:
                _send_ws(conn, b"\x03\xe8", opcode=0x8)
            except Exception:
                pass
            self._audit(
                "exec.end",
                namespace=namespace,
                pod=pod_name,
                exit_code=exit_code,
            )
            if exec_sock:
                try:
                    exec_sock.close()
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
        namespace: str | None,
        pod_name: str,
        command: list[str],
        container: str | None,
        tty: bool,
        want_stdin: bool,
        want_stdout: bool,
        want_stderr: bool,
        runtime: RuntimeAdapter | None = None,
    ) -> None:
        spdy_debug = SPDY_DEBUG or str(os.getenv("AE_APISHIM_SPDY_DEBUG", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if spdy_debug:
            msg = (
                f"SPDY exec start pod={pod_name} container={container} tty={tty} "
                f"cmd={' '.join(command)}"
            )
            LOGGER.warning(msg)
            print(msg, file=sys.stderr, flush=True)
            _spdy_debug_line(msg)
        self._audit(
            "exec.start",
            namespace=namespace,
            pod=pod_name,
            container=container or "",
            tty=tty,
            stdin=want_stdin,
            stdout=want_stdout,
            stderr=want_stderr,
            cmd=" ".join(command),
        )
        rt = runtime or self.server.runtime  # type: ignore[attr-defined]
        # Try to open an attached exec session on the runtime (docker/podman only for now)
        exec_sock = None
        exec_id = None
        if hasattr(rt, "exec_attach"):
            try:
                exec_sock, exec_id = rt.exec_attach(  # type: ignore[attr-defined]
                    pod_name, command, container=container, tty=tty
                )
                exec_sock.settimeout(0.05)
            except Exception as exc:
                if SPDY_DEBUG:
                    LOGGER.warning("SPDY exec_attach failed: %s", exc)
                exec_sock = None
        if exec_sock is None:
            self._json_status(
                HTTPStatus.NOT_IMPLEMENTED,
                reason="NotImplemented",
                message="Streaming exec not available for this runtime",
            )
            return

        def _parse_header_values(name: str) -> list[str]:
            values = self.headers.get_all(name) if hasattr(self.headers, "get_all") else None
            if not values:
                val = self.headers.get(name)
                values = [val] if val else []
            parsed: list[str] = []
            for value in values:
                if not value:
                    continue
                for part in value.split(","):
                    proto = part.strip()
                    if proto:
                        parsed.append(proto)
            return parsed

        server_protocols = [
            "v5.channel.k8s.io",
            "v4.channel.k8s.io",
            "v3.channel.k8s.io",
            "v2.channel.k8s.io",
            "channel.k8s.io",
        ]
        client_protocols = _parse_header_values("X-Stream-Protocol-Version")
        stream_proto = ""
        for proto in client_protocols:
            if proto in server_protocols:
                stream_proto = proto
                break
        if not stream_proto:
            self.send_response(403, "Forbidden")
            for proto in server_protocols:
                self.send_header("X-Accepted-Stream-Protocol-Versions", proto)
            self.end_headers()
            if SPDY_DEBUG:
                LOGGER.warning(
                    "SPDY exec handshake failed client_protocols=%s",
                    client_protocols,
                )
            return

        # Accept upgrade after we know we can serve it
        self.send_response(101, "Switching Protocols")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", self.headers.get("Upgrade", "SPDY/3.1"))
        self.send_header("X-Stream-Protocol-Version", stream_proto)
        if spdy_debug:
            msg = f"SPDY exec stream-proto={stream_proto}"
            LOGGER.warning(msg)
            print(msg, file=sys.stderr, flush=True)
            _spdy_debug_line(msg)
        self.end_headers()

        conn = self.connection
        conn.settimeout(0.05)

        SPDY_DICT = base64.b64decode(
            "AAAAB29wdGlvbnMAAAAEaGVhZAAAAARwb3N0AAAAA3B1dAAAAAZkZWxldGUAAAAFdHJhY2UAAAAGYWNjZXB0AAAADmFjY2VwdC1jaGFyc2V0AAAAD2FjY2VwdC1lbmNvZGluZwAAAA9hY2NlcHQtbGFuZ3VhZ2UAAAANYWNjZXB0LXJhbmdlcwAAAANhZ2UAAAAFYWxsb3cAAAANYXV0aG9yaXphdGlvbgAAAA1jYWNoZS1jb250cm9sAAAACmNvbm5lY3Rpb24AAAAMY29udGVudC1iYXNlAAAAEGNvbnRlbnQtZW5jb2RpbmcAAAAQY29udGVudC1sYW5ndWFnZQAAAA5jb250ZW50LWxlbmd0aAAAABBjb250ZW50LWxvY2F0aW9uAAAAC2NvbnRlbnQtbWQ1AAAADWNvbnRlbnQtcmFuZ2UAAAAMY29udGVudC10eXBlAAAABGRhdGUAAAAEZXRhZwAAAAZleHBlY3QAAAAHZXhwaXJlcwAAAARmcm9tAAAABGhvc3QAAAAIaWYtbWF0Y2gAAAARaWYtbW9kaWZpZWQtc2luY2UAAAANaWYtbm9uZS1tYXRjaAAAAAhpZi1yYW5nZQAAABNpZi11bm1vZGlmaWVkLXNpbmNlAAAADWxhc3QtbW9kaWZpZWQAAAAIbG9jYXRpb24AAAAMbWF4LWZvcndhcmRzAAAABnByYWdtYQAAABJwcm94eS1hdXRoZW50aWNhdGUAAAATcHJveHktYXV0aG9yaXphdGlvbgAAAAVyYW5nZQAAAAdyZWZlcmVyAAAAC3JldHJ5LWFmdGVyAAAABnNlcnZlcgAAAAJ0ZQAAAAd0cmFpbGVyAAAAEXRyYW5zZmVyLWVuY29kaW5nAAAAB3VwZ3JhZGUAAAAKdXNlci1hZ2VudAAAAAR2YXJ5AAAAA3ZpYQAAAAd3YXJuaW5nAAAAEHd3dy1hdXRoZW50aWNhdGUAAAAGbWV0aG9kAAAAA2dldAAAAAZzdGF0dXMAAAAGMjAwIE9LAAAAB3ZlcnNpb24AAAAISFRUUC8xLjEAAAADdXJsAAAABnB1YmxpYwAAAApzZXQtY29va2llAAAACmtlZXAtYWxpdmUAAAAGb3JpZ2luMTAwMTAxMjAxMjAyMjA1MjA2MzAwMzAyMzAzMzA0MzA1MzA2MzA3NDAyNDA1NDA2NDA3NDA4NDA5NDEwNDExNDEyNDEzNDE0NDE1NDE2NDE3NTAyNTA0NTA1MjAzIE5vbi1BdXRob3JpdGF0aXZlIEluZm9ybWF0aW9uMjA0IE5vIENvbnRlbnQzMDEgTW92ZWQgUGVybWFuZW50bHk0MDAgQmFkIFJlcXVlc3Q0MDEgVW5hdXRob3JpemVkNDAzIEZvcmJpZGRlbjQwNCBOb3QgRm91bmQ1MDAgSW50ZXJuYWwgU2VydmVyIEVycm9yNTAxIE5vdCBJbXBsZW1lbnRlZDUwMyBTZXJ2aWNlIFVuYXZhaWxhYmxlSmFuIEZlYiBNYXIgQXByIE1heSBKdW4gSnVsIEF1ZyBTZXB0IE9jdCBOb3YgRGVjIDAwOjAwOjAwIE1vbiwgVHVlLCBXZWQsIFRodSwgRnJpLCBTYXQsIFN1biwgR01UY2h1bmtlZCx0ZXh0L2h0bWwsaW1hZ2UvcG5nLGltYWdlL2pwZyxpbWFnZS9naWYsYXBwbGljYXRpb24veG1sLGFwcGxpY2F0aW9uL3hodG1sK3htbCx0ZXh0L3BsYWluLHRleHQvamF2YXNjcmlwdCxwdWJsaWNwcml2YXRlbWF4LWFnZT1nemlwLGRlZmxhdGUsc2RjaGNoYXJzZXQ9dXRmLThjaGFyc2V0PWlzby04ODU5LTEsdXRmLSwqLGVucT0wLg=="
        )
        dctx = zlib.decompressobj(wbits=15, zdict=SPDY_DICT)
        cctx = zlib.compressobj(wbits=15, zdict=SPDY_DICT)

        stream_ids: dict[str, int] = {}  # streamtype -> sid
        fallback_streams = ["stdin", "stdout", "stderr", "error", "resize"]
        fallback_idx = 0
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

        def send_settings(initial_window: int | None = None) -> None:
            entries = bytearray()
            num_entries = 0
            if initial_window is not None:
                num_entries = 1
                entries.append(0x00)  # flags
                entries += (0x04).to_bytes(3, "big")  # SETTINGS_INITIAL_WINDOW_SIZE
                entries += int(initial_window).to_bytes(4, "big")
            payload = num_entries.to_bytes(4, "big") + entries
            header = bytearray()
            header += b"\x80\x03"
            header += (0x04).to_bytes(2, "big")
            header += b"\x00"
            header += len(payload).to_bytes(3, "big")
            conn.sendall(bytes(header) + payload)

        try:
            send_settings(initial_window=window_size)
            if SPDY_DEBUG:
                LOGGER.warning("SPDY sent SETTINGS initial_window=%s", window_size)
        except Exception as exc:
            if SPDY_DEBUG:
                LOGGER.warning("SPDY failed to send SETTINGS: %s", exc)

        def _encode_headers(headers: dict[str, str]) -> bytes:
            buf = bytearray()
            buf += len(headers).to_bytes(4, "big")
            for name, value in headers.items():
                n = name.encode("utf-8")
                v = value.encode("utf-8")
                buf += len(n).to_bytes(4, "big")
                buf += n
                buf += len(v).to_bytes(4, "big")
                buf += v
            return cctx.compress(bytes(buf)) + cctx.flush(zlib.Z_SYNC_FLUSH)

        def send_syn_reply(stream_id: int) -> None:
            hdrs = _encode_headers({":status": "200", ":version": "HTTP/1.1"})
            header = bytearray()
            header += b"\x80\x03"
            header += (0x02).to_bytes(2, "big")  # SYN_REPLY
            header += b"\x00"
            header += (len(hdrs) + 4).to_bytes(3, "big")
            header += (stream_id & 0x7FFFFFFF).to_bytes(4, "big")
            conn.sendall(bytes(header) + hdrs)

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
                if SPDY_DEBUG:
                    LOGGER.warning("SPDY exec syn-parse failed len=%s", len(payload))
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
            data = frame[8 : 8 + size]
            if len(data) < size:
                return None
            return stream_type, data

        spdy_buf = b""
        last_ping = time.time()
        exec_done = False
        exec_done_at = 0.0
        exec_grace_seconds = 3.0
        max_seconds, idle_seconds = self._stream_limits()
        max_bytes = self._stream_byte_limit()
        bytes_in = 0
        bytes_out = 0
        start_ts = time.time()
        last_activity = start_ts
        required_streams: set[str] = {"error"}
        if want_stdout:
            required_streams.add("stdout")
        if want_stderr and not tty:
            required_streams.add("stderr")
        if want_stdin:
            required_streams.add("stdin")
        if tty:
            required_streams.add("resize")
        warned_missing_streams = False
        break_reason = ""
        try:
            exec_buf = b""
            while True:
                now = time.time()
                if max_seconds and (now - start_ts) > max_seconds:
                    break_reason = "max_seconds"
                    break
                if idle_seconds and (now - last_activity) > idle_seconds:
                    break_reason = "idle_timeout"
                    break
                if max_bytes and (bytes_in + bytes_out) > max_bytes:
                    break_reason = "max_bytes"
                    break
                if now - last_ping > 10:
                    try:
                        send_ping()
                    except Exception:
                        break_reason = "ping_failed"
                        break
                    last_ping = now

                # Read SPDY control/data frames from client (buffered)
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    chunk = None
                except Exception as exc:
                    if not break_reason:
                        break_reason = f"client_recv_error:{type(exc).__name__}"
                    chunk = b""
                if chunk == b"":
                    if not break_reason:
                        break_reason = "client_eof"
                    break
                if chunk:
                    last_activity = time.time()
                    spdy_buf += chunk

                while True:
                    if len(spdy_buf) < 8:
                        break
                    hdr = spdy_buf[:8]
                    is_control = (hdr[0] & 0x80) != 0
                    length = int.from_bytes(hdr[5:8], "big")
                    frame_len = 8 + length
                    if len(spdy_buf) < frame_len:
                        break
                    payload = spdy_buf[8:frame_len]
                    spdy_buf = spdy_buf[frame_len:]
                    if is_control:
                        frame_type = int.from_bytes(hdr[2:4], "big")
                        flags = hdr[4]
                        if SPDY_DEBUG:
                            LOGGER.warning(
                                "SPDY ctrl frame type=%s flags=0x%02x length=%s",
                                frame_type,
                                flags,
                                length,
                            )
                        if length > (1 << 20):
                            send_goaway(status=2)
                            break
                        if frame_type == 1:  # SYN_STREAM registers channels
                            sid = int.from_bytes(payload[0:4], "big") & 0x7FFFFFFF
                            if SPDY_DEBUG:
                                LOGGER.warning(
                                    "SPDY exec syn-raw len=%s data=%s",
                                    len(payload),
                                    payload[:40].hex(),
                                )
                            headers = parse_syn_stream(payload)
                            stype = headers.get("streamtype", "").strip().lower()
                            if SPDY_DEBUG:
                                LOGGER.warning("SPDY SYN_STREAM sid=%s headers=%s", sid, headers)
                            if stype not in {"stdin", "stdout", "stderr", "error", "resize"}:
                                if fallback_idx < len(fallback_streams):
                                    stype = fallback_streams[fallback_idx]
                                    fallback_idx += 1
                                    if SPDY_DEBUG:
                                        LOGGER.warning(
                                            "SPDY streamtype fallback sid=%s assigned=%s",
                                            sid,
                                            stype,
                                        )
                            if stype:
                                stream_ids[stype] = sid
                            stream_windows[sid] = window_size
                            if stype == "resize":
                                resize_sid = sid
                            try:
                                send_syn_reply(sid)
                            except Exception:
                                pass
                        elif frame_type == 4:  # SETTINGS
                            try:
                                num = int.from_bytes(payload[0:4], "big")
                                idx = 4
                                for _ in range(num):
                                    if idx + 8 > len(payload):
                                        break
                                    _flags = payload[idx]
                                    sid_setting = int.from_bytes(payload[idx + 1 : idx + 3], "big")
                                    val = int.from_bytes(payload[idx + 3 : idx + 7], "big")
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
                        if SPDY_DEBUG:
                            LOGGER.warning(
                                "SPDY data frame sid=%s flags=0x%02x length=%s",
                                stream_id,
                                flags,
                                length,
                            )
                        bytes_in += length
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
                                h = (
                                    int(doc.get("Height"))
                                    if doc.get("Height") is not None
                                    else None
                                )
                                w = int(doc.get("Width")) if doc.get("Width") is not None else None
                                if hasattr(rt, "exec_resize"):
                                    try:
                                        rt.exec_resize(exec_id or "", height=h, width=w)  # type: ignore[attr-defined]
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
                if exec_sock:
                    try:
                        chunk = exec_sock.recv(4096)
                    except TimeoutError:
                        chunk = None
                    except Exception:
                        chunk = b""
                    if chunk:
                        last_activity = time.time()
                        bytes_out += len(chunk)
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
                        exec_done = True
                        exec_done_at = time.time()
                        try:
                            exec_sock.close()
                        except Exception:
                            pass
                        exec_sock = None

                if exec_done:
                    missing = required_streams.difference(stream_ids.keys())
                    if missing and SPDY_DEBUG and not warned_missing_streams:
                        warned_missing_streams = True
                        LOGGER.warning("SPDY exec missing streams after exit: %s", sorted(missing))
                    if not missing or (time.time() - exec_done_at) > exec_grace_seconds:
                        break_reason = "exec_done"
                        break

        finally:
            missing = required_streams.difference(stream_ids.keys())
            if spdy_debug:
                msg = (
                    "SPDY exec end reason="
                    f"{break_reason or 'unknown'} pod={pod_name} container={container or ''} "
                    f"bytes_in={bytes_in} bytes_out={bytes_out} streams={sorted(stream_ids.keys())} "
                    f"missing={sorted(missing)}"
                )
                LOGGER.warning(msg)
                print(msg, file=sys.stderr, flush=True)
                _spdy_debug_line(msg)
            elif missing:
                LOGGER.warning(
                    "SPDY exec end missing streams reason=%s pod=%s container=%s bytes_in=%s bytes_out=%s streams=%s missing=%s",
                    break_reason or "unknown",
                    pod_name,
                    container or "",
                    bytes_in,
                    bytes_out,
                    sorted(stream_ids.keys()),
                    sorted(missing),
                )
            elif break_reason and break_reason != "exec_done":
                LOGGER.warning(
                    "SPDY exec end reason=%s pod=%s container=%s bytes_in=%s bytes_out=%s streams=%s missing=%s",
                    break_reason,
                    pod_name,
                    container or "",
                    bytes_in,
                    bytes_out,
                    sorted(stream_ids.keys()),
                    sorted(missing),
                )
            elif SPDY_DEBUG:
                LOGGER.warning(
                    "SPDY exec end reason=%s pod=%s container=%s bytes_in=%s bytes_out=%s streams=%s missing=%s",
                    break_reason or "unknown",
                    pod_name,
                    container or "",
                    bytes_in,
                    bytes_out,
                    sorted(stream_ids.keys()),
                    sorted(missing),
                )
            # Send exit status over error stream if present
            exit_code = 0
            try:
                if exec_id and hasattr(rt, "exec_exit_code"):
                    exit_code = int(rt.exec_exit_code(exec_id))  # type: ignore[attr-defined]
            except Exception:
                exit_code = 0
            err_sid = stream_ids.get("error")
            if err_sid:
                status_obj = _exec_status_obj(exit_code)
                try:
                    send_data_frame(
                        err_sid,
                        json.dumps(status_obj, separators=(",", ":")).encode("utf-8"),
                        flags=0x02,
                    )
                except Exception:
                    pass
            self._audit(
                "exec.end",
                namespace=namespace,
                pod=pod_name,
                exit_code=exit_code,
            )
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
        label_sel, field_sel = _selector_values_from_query(query)
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
                bm = {
                    "type": "BOOKMARK",
                    "object": {"metadata": {"resourceVersion": str(initial_rv)}},
                }
                self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()
            for ev_type, obj in self.server.store.watch(
                group,
                version,
                resource,
                namespace,
                heartbeat_seconds=heartbeat,
                allow_bookmarks=allow_bm,
                since_rv=int(rv_param) if rv_param and rv_param.isdigit() else None,
            ):  # type: ignore[attr-defined]
                if ev_type != "BOOKMARK" and not _matches_selectors(obj, label_sel, field_sel):
                    continue
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
            rv = max(
                (int(o.get("metadata", {}).get("resourceVersion", "0")) for o in objs), default=0
            )
            bm = {
                "type": "BOOKMARK",
                "object": {
                    "kind": kind,
                    "apiVersion": api_version,
                    "metadata": {"resourceVersion": str(rv)},
                },
            }
            self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def _serve_dynamic_group_discovery(self, path: str) -> bool:
        self._refresh_crd_registry_from_state()
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
                            "preferredVersion": {
                                "groupVersion": "discovery.k8s.io/v1",
                                "version": "v1",
                            },
                            "serverAddressByClientCIDRs": [],
                        }
                    )
                    return True
                if group == "storage.k8s.io":
                    self._ok(
                        {
                            "kind": "APIGroup",
                            "apiVersion": "v1",
                            "name": "storage.k8s.io",
                            "versions": [{"groupVersion": "storage.k8s.io/v1", "version": "v1"}],
                            "preferredVersion": {
                                "groupVersion": "storage.k8s.io/v1",
                                "version": "v1",
                            },
                            "serverAddressByClientCIDRs": [],
                        }
                    )
                    return True
                if group == "snapshot.storage.k8s.io":
                    self._ok(
                        {
                            "kind": "APIGroup",
                            "apiVersion": "v1",
                            "name": "snapshot.storage.k8s.io",
                            "versions": [
                                {
                                    "groupVersion": "snapshot.storage.k8s.io/v1",
                                    "version": "v1",
                                }
                            ],
                            "preferredVersion": {
                                "groupVersion": "snapshot.storage.k8s.io/v1",
                                "version": "v1",
                            },
                            "serverAddressByClientCIDRs": [],
                        }
                    )
                    return True
                return False
            payload = {
                "kind": "APIGroup",
                "apiVersion": "v1",
                "name": group,
                "versions": [
                    {"groupVersion": f"{group}/{ver}", "version": ver} for ver in versions
                ],
                "preferredVersion": {
                    "groupVersion": f"{group}/{versions[0]}",
                    "version": versions[0],
                },
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
            if group == "storage.k8s.io" and version == "v1":
                self._ok(
                    {
                        "kind": "APIResourceList",
                        "apiVersion": "storage.k8s.io/v1",
                        "groupVersion": "storage.k8s.io/v1",
                        "resources": [
                            {
                                "name": "storageclasses",
                                "singularName": "storageclass",
                                "namespaced": False,
                                "kind": "StorageClass",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                                "shortNames": ["sc"],
                            },
                            {
                                "name": "volumeattachments",
                                "singularName": "volumeattachment",
                                "namespaced": False,
                                "kind": "VolumeAttachment",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                            },
                            {
                                "name": "csidrivers",
                                "singularName": "csidriver",
                                "namespaced": False,
                                "kind": "CSIDriver",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                            },
                            {
                                "name": "csinodes",
                                "singularName": "csinode",
                                "namespaced": False,
                                "kind": "CSINode",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                            },
                            {
                                "name": "csistoragecapacities",
                                "singularName": "csistoragecapacity",
                                "namespaced": True,
                                "kind": "CSIStorageCapacity",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                            },
                        ],
                    }
                )
                return True
            if group == "snapshot.storage.k8s.io" and version == "v1":
                self._ok(
                    {
                        "kind": "APIResourceList",
                        "apiVersion": "snapshot.storage.k8s.io/v1",
                        "groupVersion": "snapshot.storage.k8s.io/v1",
                        "resources": [
                            {
                                "name": "volumesnapshots",
                                "singularName": "volumesnapshot",
                                "namespaced": True,
                                "kind": "VolumeSnapshot",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                                "shortNames": ["vs"],
                            },
                            {
                                "name": "volumesnapshotclasses",
                                "singularName": "volumesnapshotclass",
                                "namespaced": False,
                                "kind": "VolumeSnapshotClass",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                                "shortNames": ["vsc"],
                            },
                            {
                                "name": "volumesnapshotcontents",
                                "singularName": "volumesnapshotcontent",
                                "namespaced": False,
                                "kind": "VolumeSnapshotContent",
                                "verbs": [
                                    "get",
                                    "list",
                                    "create",
                                    "delete",
                                    "patch",
                                    "update",
                                    "watch",
                                ],
                                "shortNames": ["vscnt"],
                            },
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

    @classmethod
    def _refresh_crd_registry_from_state(cls, *, force: bool = False) -> None:
        ha_mode = str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}
        if not ha_mode:
            return
        refresh_interval = max(
            0.1,
            float(os.getenv("AE_APISHIM_HA_CRD_REFRESH_SEC", "0.5") or "0.5"),
        )
        now = time.monotonic()
        with cls.crd_lock:
            if not force and (now - cls._crd_refresh_monotonic) < refresh_interval:
                return
        state = getattr(cls, "state", None)
        if state is None or not hasattr(state, "list_authority_objects"):
            return
        try:
            objs = state.list_authority_objects(
                "apiextensions.k8s.io",
                "v1",
                "customresourcedefinitions",
                None,
            )
        except Exception:
            return
        with cls.crd_lock:
            cls.crd_registry = {}
            cls.crd_index = {}
        for obj in objs:
            cls._register_crd(
                K8sObject(
                    "apiextensions.k8s.io",
                    "v1",
                    "customresourcedefinitions",
                    None,
                    obj.name,
                    dict(obj.metadata or {}),
                    dict(obj.spec or {}),
                    dict(obj.status or {}),
                    int(obj.resource_version or 0),
                )
            )
        with cls.crd_lock:
            cls._crd_refresh_monotonic = now

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        upgrade = (self.headers.get("Upgrade") or "").lower()
        is_exec_path = re.match(r"^/api/v1/namespaces/[^/]+/pods/[^/]+/exec$", path)
        is_pf_path = re.match(r"^/api/v1/namespaces/[^/]+/(pods|services)/[^/]+/portforward$", path)
        # Allow unauthenticated discovery/OpenAPI for kubectl validation
        if path not in {"/openapi/v2", "/openapi/v3", "/swagger.json", "/api", "/apis", "/version"}:
            if is_exec_path and upgrade:
                if not self._authz(role="exec"):
                    return
            elif is_pf_path and upgrade:
                if not self._authz(role="portforward"):
                    return
            else:
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
            self._refresh_crd_registry_from_state()
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
                    "versions": [{"groupVersion": "networking.k8s.io/v1", "version": "v1"}],
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
                    "name": "storage.k8s.io",
                    "versions": [{"groupVersion": "storage.k8s.io/v1", "version": "v1"}],
                    "preferredVersion": {
                        "groupVersion": "storage.k8s.io/v1",
                        "version": "v1",
                    },
                },
                {
                    "name": "snapshot.storage.k8s.io",
                    "versions": [
                        {
                            "groupVersion": "snapshot.storage.k8s.io/v1",
                            "version": "v1",
                        }
                    ],
                    "preferredVersion": {
                        "groupVersion": "snapshot.storage.k8s.io/v1",
                        "version": "v1",
                    },
                },
                {
                    "name": "rbac.authorization.k8s.io",
                    "versions": [{"groupVersion": "rbac.authorization.k8s.io/v1", "version": "v1"}],
                    "preferredVersion": {
                        "groupVersion": "rbac.authorization.k8s.io/v1",
                        "version": "v1",
                    },
                },
                {
                    "name": "authorization.k8s.io",
                    "versions": [{"groupVersion": "authorization.k8s.io/v1", "version": "v1"}],
                    "preferredVersion": {
                        "groupVersion": "authorization.k8s.io/v1",
                        "version": "v1",
                    },
                },
                {
                    "name": "policy",
                    "versions": [{"groupVersion": "policy/v1", "version": "v1"}],
                    "preferredVersion": {"groupVersion": "policy/v1", "version": "v1"},
                },
                {
                    "name": "autoscaling",
                    "versions": [{"groupVersion": "autoscaling/v2", "version": "v2"}],
                    "preferredVersion": {
                        "groupVersion": "autoscaling/v2",
                        "version": "v2",
                    },
                },
                {
                    "name": "apiextensions.k8s.io",
                    "versions": [{"groupVersion": "apiextensions.k8s.io/v1", "version": "v1"}],
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
                            {"groupVersion": f"{dyn}/{ver}", "version": ver} for ver in versions
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                            "shortNames": ["cm"],
                        },
                        {
                            "name": "secrets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Secret",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                        },
                        {
                            "name": "persistentvolumeclaims",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "PersistentVolumeClaim",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                            "shortNames": ["pvc"],
                        },
                        {
                            "name": "persistentvolumes",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "PersistentVolume",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                            "shortNames": ["pv"],
                        },
                        {
                            "name": "serviceaccounts",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ServiceAccount",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                            "shortNames": ["sa"],
                        },
                        {
                            "name": "services",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "Service",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                        {
                            "name": "replicasets",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ReplicaSet",
                            "verbs": ["get", "list", "watch"],
                            "shortNames": ["rs"],
                        },
                        {
                            "name": "replicasets/status",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "ReplicaSet",
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                        },
                        {
                            "name": "rolebindings",
                            "singularName": "",
                            "namespaced": True,
                            "kind": "RoleBinding",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                        },
                        {
                            "name": "clusterroles",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRole",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
                        },
                        {
                            "name": "clusterrolebindings",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "ClusterRoleBinding",
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                        },
                        {
                            "name": "selfsubjectaccessreviews",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "SelfSubjectAccessReview",
                            "verbs": ["create"],
                        },
                        {
                            "name": "selfsubjectrulesreviews",
                            "singularName": "",
                            "namespaced": False,
                            "kind": "SelfSubjectRulesReview",
                            "verbs": ["create"],
                        },
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
                            "verbs": [
                                "get",
                                "list",
                                "create",
                                "delete",
                                "patch",
                                "update",
                                "watch",
                            ],
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
        if plural in {
            "namespaces",
            "configmaps",
            "secrets",
            "persistentvolumeclaims",
            "persistentvolumes",
            "serviceaccounts",
            "services",
        }:
            if name is None:
                label_sel, field_sel = _selector_values_from_query(q)
                # watch support on LIST endpoints
                if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                    if not self._rbac_allows("watch", plural):
                        self._deny(403)
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        start = time.time()
                        timeout = int(q.get("timeoutSeconds", ["0"])[0] or 0) or None
                        heartbeat = int(q.get("heartbeatSeconds", ["0"])[0] or 0) or None
                        allow_bm = q.get("allowWatchBookmarks", ["0"])[0] in ("1", "true", "True")
                        for ev_type, obj in self.server.store.watch(
                            "",
                            "v1",
                            plural,
                            ns,
                            heartbeat_seconds=heartbeat,
                            allow_bookmarks=allow_bm,
                        ):  # type: ignore[attr-defined]
                            if ev_type != "BOOKMARK" and not _matches_selectors(
                                obj, label_sel, field_sel
                            ):
                                continue
                            line = (
                                json.dumps(
                                    {"type": ev_type, "object": _to_obj(obj)}, separators=(",", ":")
                                ).encode("utf-8")
                                + b"\n"
                            )
                            self.wfile.write(line)
                            self.wfile.flush()
                            if (
                                timeout is not None
                                and timeout > 0
                                and (time.time() - start) >= timeout
                            ):
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
                items = _filter_k8s_items(items, label_sel, field_sel)

                def _transform(obj: K8sObject) -> dict[str, Any]:
                    if plural == "services":
                        doc = _to_obj(obj)
                        doc = _merge_provider_service(
                            self.server.state, self.server.store, doc, obj
                        )  # type: ignore[attr-defined]
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
                obj = self.server.store.get(
                    "", "v1", plural, None if plural == "namespaces" else ns, name
                )  # type: ignore[attr-defined]
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
                    doc = _merge_provider_service(
                        self.server.state, self.server.store, _to_obj(obj), obj
                    )  # type: ignore[attr-defined]
                    self._ok(doc)
                    return
                self._ok(_to_obj(obj))
                return
            # Mutations
            if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                verb = {"POST": "create", "PUT": "update", "PATCH": "patch", "DELETE": "delete"}[
                    self.command
                ]
                if not self._rbac_allows(verb, plural):
                    self._deny(403)
                    return
        if plural in {
            "namespaces",
            "configmaps",
            "secrets",
            "persistentvolumeclaims",
            "persistentvolumes",
            "serviceaccounts",
            "services",
        }:
            # Mutations
            pass
        # Endpoints (projected from controller state)
        if plural == "endpoints":
            if q.get("watch", ["0"])[0] in ("1", "true", "True"):
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
                    ep = _endpoints_for_service(self.server.state, self.server.store, svc)  # type: ignore[attr-defined]
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
                self._ok(
                    {
                        "kind": "EndpointsList",
                        "apiVersion": "v1",
                        "metadata": meta,
                        "items": selected,
                    }
                )
                return
            svc = self.server.store.get("", "v1", "services", ns, name)  # type: ignore[attr-defined]
            if not svc:
                self._not_found()
                return
            ep = _endpoints_for_service(self.server.state, self.server.store, svc)  # type: ignore[attr-defined]
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
            label_sel, field_sel = _selector_values_from_query(q)
            # enrich with controller pod/node info when available
            replica_info: dict[str, tuple[str | None, bool, bool, str, str, str]] = {}
            try:
                # Build once for all apps to avoid N+1 queries
                for app in {(c.get("labels", {}) or {}).get("ae.app") for c in containers}:
                    if not app:
                        continue
                    rows = []
                    try:
                        rows = self.server.state.list_pod_nodes(app)  # type: ignore[attr-defined]
                    except Exception:
                        rows = []
                    for rid, node_id, ready, live, status, rmsg, lmsg in rows:
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
                rid = labels.get("ae.pod_name") or labels.get("ae.replica_id") or c.get("name")
                rep_info = replica_info.get(str(rid))
                node_name = labels.get("ae.node") or (rep_info[0] if rep_info else None)
                pod_obj = _pod_obj(c, now_rv, node_name)
                if not _matches_selectors(pod_obj, label_sel, field_sel):
                    continue
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
                        cs["state"] = {
                            "waiting": {"reason": rep_info[3] or "Pending", "message": rep_info[4]}
                        }
                pod_objs.append(pod_obj)
            self._update_pod_watch_cache(pod_objs, now_rv)
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
                        self.wfile.write(
                            json.dumps(ev, separators=(",", ":")).encode("utf-8") + b"\n"
                        )
                    bm = {
                        "type": "BOOKMARK",
                        "object": {
                            "kind": "Pod",
                            "apiVersion": "v1",
                            "metadata": {"resourceVersion": str(now_rv)},
                        },
                    }
                    self.wfile.write(json.dumps(bm, separators=(",", ":")).encode("utf-8") + b"\n")
                    self.wfile.flush()
                    # Keep the watch stream open until client disconnects
                    while True:
                        time.sleep(5)
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
                self._ok(
                    {"kind": "PodList", "apiVersion": "v1", "metadata": meta, "items": selected}
                )
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
                    for ln in self.server.runtime.read_logs(
                        pod_name, follow=True, tail=tail_i, since=since_i
                    ):  # type: ignore[attr-defined]
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
                    lines = list(
                        self.server.runtime.read_logs(
                            pod_name, follow=False, tail=tail_i, since=since_i
                        )
                    )  # type: ignore[attr-defined]
                    body = b"".join([_emit(ln) for ln in lines])
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except Exception as exc:
                self._json_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc)
                )
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
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="command query param is required",
                )
                return
            expected_uid = self._extract_pod_uid(qs)
            expected_rv = self._extract_pod_rv(qs)
            container_info = self._validate_pod_scope(
                namespace=m_exec.group(1),
                pod_name=m_exec.group(2),
                scope_env="AE_API_EXEC_SCOPE",
                action="exec",
                expected_uid=expected_uid,
                expected_rv=expected_rv,
            )
            if container_info is None:
                return
            exec_runtime, _node_id, _endpoint = self._runtime_for_pod(
                m_exec.group(1), m_exec.group(2), container_info=container_info
            )
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                container = (qs.get("container") or [None])[0]
                tty = (qs.get("tty") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdin = (qs.get("stdin") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdout = (qs.get("stdout") or ["true"])[0].lower() in ("1", "true", "yes")
                want_stderr = (qs.get("stderr") or ["true"])[0].lower() in ("1", "true", "yes")
                self._handle_exec_spdy(
                    namespace=m_exec.group(1),
                    pod_name=m_exec.group(2),
                    command=list(cmd),
                    container=container,
                    tty=tty,
                    want_stdin=want_stdin,
                    want_stdout=want_stdout,
                    want_stderr=want_stderr,
                    runtime=exec_runtime,
                )
                return
            elif upgrade == "websocket":
                container = (qs.get("container") or [None])[0]
                tty = (qs.get("tty") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdin = (qs.get("stdin") or ["false"])[0].lower() in ("1", "true", "yes")
                want_stdout = (qs.get("stdout") or ["true"])[0].lower() in ("1", "true", "yes")
                want_stderr = (qs.get("stderr") or ["true"])[0].lower() in ("1", "true", "yes")
                self._handle_exec_ws(
                    namespace=m_exec.group(1),
                    pod_name=m_exec.group(2),
                    command=list(cmd),
                    container=container,
                    tty=tty,
                    want_stdin=want_stdin,
                    want_stdout=want_stdout,
                    want_stderr=want_stderr,
                    runtime=exec_runtime,
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
            if not self._validate_service_pf_scope(ns, svc, svc_name):
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
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="ports query param required",
                )
                return
            app_name = _service_app_name(svc, self.server.store)
            eps_raw = self.server.state.list_service_endpoints(app_name) if app_name else []  # type: ignore[attr-defined]
            target_ip = _pick_endpoint_ip(
                eps_raw, key=",".join(str(p) for p in target_ports) if target_ports else None
            )
            if isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_ip = os.getenv("AE_STUB_BACKEND_HOST", target_ip or "127.0.0.1")
                try:
                    target_ports = [int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))]
                except Exception:
                    target_ports = [8081]
            if not target_ip:
                self._json_status(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    reason="NoEndpoints",
                    message="no ready endpoints for service",
                )
                return
            upstream_factory = None
            try:
                node_rec = self._node_record_for_ip(str(target_ip))
            except Exception:
                node_rec = None
            if node_rec and getattr(node_rec, "endpoint", None):
                svc_runtime = self._runtime_for_endpoint(getattr(node_rec, "endpoint", None))
                if hasattr(svc_runtime, "port_forward_socket"):
                    container = self._container_for_pod_ip(svc_runtime, ns, str(target_ip))
                    if container:
                        pod_id = container.get("uid") or container.get("id")
                        labels = container.get("labels", {}) or {}
                        pod_name = (
                            labels.get("ae.pod_name")
                            or labels.get("ae.replica_id")
                            or container.get("name")
                        )

                        # Use node agent port-forward when available.

                        def _pf_open(port: int, _rt=svc_runtime) -> socket.socket | None:
                            try:
                                return _rt.port_forward_socket(  # type: ignore[attr-defined]
                                    pod_id=str(pod_id) if pod_id else None,
                                    pod_name=str(pod_name) if pod_name else None,
                                    namespace=ns,
                                    port=int(port),
                                )
                            except Exception:
                                return None

                        upstream_factory = _pf_open
            upgrade = (self.headers.get("Upgrade") or "").lower()
            port_label = ",".join(str(p) for p in target_ports)
            if upgrade.startswith("spdy"):
                # choose endpoint per target port to spread load
                ep_map: dict[int, list[str]] = {}
                for tp in target_ports:
                    # include all ready endpoints for port spread
                    port_ips = [ep.ip for ep in eps_raw if ep.ready] or [ep.ip for ep in eps_raw]
                    if port_ips:
                        ep_map[tp] = port_ips
                # fallback: single target_ip if map empty
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    service=svc_name,
                    ports=port_label,
                    protocol="spdy",
                )
                try:
                    self._handle_port_forward_spdy(
                        target_ip,
                        target_ports,
                        ep_map if ep_map else None,
                        upstream_factory=upstream_factory,
                    )
                finally:
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        service=svc_name,
                        ports=port_label,
                    )
            elif upgrade == "websocket":
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    service=svc_name,
                    ports=port_label,
                    protocol="websocket",
                )
                try:
                    self._handle_port_forward_ws(
                        target_ip, target_ports[0], upstream_factory=upstream_factory
                    )
                finally:
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        service=svc_name,
                        ports=port_label,
                    )
            else:
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="port-forward requires SPDY/3.1 used by kubectl",
                )
            return
        # Port-forward
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", path)
        if m_pf:
            if not self._rbac_allows("create", "pods/portforward"):
                self._deny(403)
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            ns = m_pf.group(1)
            pod_name = m_pf.group(2)
            container_info = self._validate_pod_scope(
                namespace=ns,
                pod_name=pod_name,
                scope_env="AE_API_PF_SCOPE",
                action="port-forward",
                expected_uid=self._extract_pod_uid(qs),
                expected_rv=self._extract_pod_rv(qs),
            )
            if container_info is None:
                return
            pf_runtime, _node_id, _endpoint = self._runtime_for_pod(
                ns, pod_name, container_info=container_info
            )
            upgrade = (self.headers.get("Upgrade") or "").lower()
            requested_ports: list[int] = []
            for p in ports_q:
                try:
                    requested_ports.append(int(p))
                except Exception:
                    pass
            target_ports = list(requested_ports)
            pod_id = None
            pod_ip = None
            host_ports: list[int] = []
            port_map: dict[int, int] = {}
            if container_info:
                default_host = "127.0.0.1"
                pod_ip = container_info.get("pod_ip")
                pod_id = container_info.get("uid") or container_info.get("id")
                host_ip = (
                    container_info.get("host_ip") or container_info.get("hostIP") or default_host
                )
                raw_host_ports = (
                    container_info.get("host_ports") or container_info.get("hostPorts") or []
                )
                for hp in raw_host_ports:
                    try:
                        host_ports.append(int(hp))
                    except Exception:
                        continue
                raw_port_map = container_info.get("port_map") or container_info.get("portMap") or {}
                if isinstance(raw_port_map, dict):
                    for cport, hport in raw_port_map.items():
                        try:
                            port_map[int(cport)] = int(hport)
                        except Exception:
                            continue

                # Prefer pod IP + container port; fall back to host ports when needed.
                if pod_ip:
                    target_host = pod_ip
                    if not target_ports:
                        if port_map:
                            target_ports = [sorted(port_map.keys())[0]]
                        elif host_ports:
                            target_host = host_ip
                            target_ports = [host_ports[0]]
                else:
                    target_host = host_ip
                    if target_ports and port_map:
                        target_ports = [port_map.get(p, p) for p in target_ports]
                    elif not target_ports:
                        if host_ports:
                            target_ports = [host_ports[0]]
                        elif port_map:
                            target_ports = [sorted(port_map.values())[0]]
            else:
                target_host = "127.0.0.1"
            use_agent_pf = (
                pf_runtime is not self.server.runtime  # type: ignore[attr-defined]
                and hasattr(pf_runtime, "port_forward_socket")
            )
            if use_agent_pf and requested_ports:
                target_ports = list(requested_ports)
            upstream_factory = None
            if use_agent_pf:

                def _pf_open(port: int, _rt=pf_runtime) -> socket.socket | None:
                    try:
                        return _rt.port_forward_socket(  # type: ignore[attr-defined]
                            pod_id=str(pod_id) if pod_id else None,
                            pod_name=str(pod_name) if pod_name else None,
                            namespace=ns,
                            port=int(port),
                        )
                    except Exception:
                        return None

                upstream_factory = _pf_open
            pf_port_map: dict[int, int] | None = None
            cri_pf_procs: list[subprocess.Popen] = []
            use_cri_pf = (
                isinstance(self.server.runtime, CRIRuntime)  # type: ignore[attr-defined]
                and pod_id
                and (
                    self._cri_pf_force()
                    or (self._cri_pf_enabled() and not pod_ip and not host_ports)
                )
            )
            if use_cri_pf and requested_ports:
                pf_port_map, cri_pf_procs = self._start_cri_port_forward(
                    str(pod_id), requested_ports
                )
                if pf_port_map:
                    target_host = "127.0.0.1"
                    target_ports = list(requested_ports)
            if isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_host = os.getenv("AE_STUB_BACKEND_HOST", target_host)
                try:
                    target_ports = [int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))]
                except Exception:
                    target_ports = [8081]
            if not target_ports:
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="ports query param required",
                )
                return
            port_label = ",".join(str(p) for p in (requested_ports or target_ports))
            if upgrade == "websocket":
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    pod=pod_name,
                    ports=port_label,
                    protocol="websocket",
                )
                try:
                    ws_port = target_ports[0]
                    if pf_port_map:
                        ws_port = pf_port_map.get(target_ports[0], ws_port)
                    self._handle_port_forward_ws(
                        target_host, ws_port, upstream_factory=upstream_factory
                    )
                finally:
                    if cri_pf_procs:
                        self._stop_cri_port_forward(cri_pf_procs)
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        pod=pod_name,
                        ports=port_label,
                    )
            elif upgrade.startswith("spdy"):
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    pod=pod_name,
                    ports=port_label,
                    protocol="spdy",
                )
                try:
                    self._handle_port_forward_spdy(
                        target_host,
                        target_ports,
                        port_map=pf_port_map,
                        upstream_factory=upstream_factory,
                    )
                finally:
                    if cri_pf_procs:
                        self._stop_cri_port_forward(cri_pf_procs)
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        pod=pod_name,
                        ports=port_label,
                    )
            else:
                if cri_pf_procs:
                    self._stop_cri_port_forward(cri_pf_procs)
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="port-forward requires SPDY/3.1 used by kubectl",
                )
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
                    line = (
                        json.dumps(
                            {
                                "type": "BOOKMARK",
                                "object": {
                                    "kind": "Event",
                                    "apiVersion": "v1",
                                    "metadata": {"resourceVersion": str(rv)},
                                },
                            },
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    self.wfile.write(line)
                    self.wfile.flush()
                except BrokenPipeError:
                    pass
                return
            # best-effort pull from stored events + controller events by namespace
            items = []
            try:
                stored = (
                    self.server.store.list_all("", "v1", "events")  # type: ignore[attr-defined]
                    if ns is None
                    else self.server.store.list("", "v1", "events", ns)  # type: ignore[attr-defined]
                )
                for ev in stored:
                    if name and ev.name != name:
                        continue
                    items.append(_to_stored_event(ev))
            except Exception:
                pass
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
                if not items:
                    items = []
            rv = int(time.time() * 1000)
            self._ok(
                {
                    "kind": "EventList",
                    "apiVersion": "v1",
                    "metadata": {"resourceVersion": str(rv)},
                    "items": items,
                }
            )
            return

        # Nodes (projected from controller state)
        if path == "/api/v1/nodes":
            nodes = self.server.state.list_nodes()  # type: ignore[attr-defined]
            now_rv = int(time.time() * 1000)
            items = []
            for idx, (rec, st) in enumerate(nodes, start=1):
                items.append(_node_obj(rec, st, now_rv + idx))
            self._ok(
                {
                    "kind": "NodeList",
                    "apiVersion": "v1",
                    "metadata": {"resourceVersion": str(now_rv)},
                    "items": items,
                }
            )
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
                    label_sel, field_sel = _selector_values_from_query(q)
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "deployments"):
                            self._deny(403)
                            return
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"])[0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"])[0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"])[0] in (
                                "1",
                                "true",
                                "True",
                            )
                            for ev_type, obj in self.server.store.watch(
                                "apps",
                                "v1",
                                "deployments",
                                d_ns,
                                heartbeat_seconds=heartbeat,
                                allow_bookmarks=allow_bm,
                            ):  # type: ignore[attr-defined]
                                if ev_type != "BOOKMARK" and not _matches_selectors(
                                    obj, label_sel, field_sel
                                ):
                                    continue
                                line = (
                                    json.dumps(
                                        {"type": ev_type, "object": _to_deployment(obj)},
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                    + b"\n"
                                )
                                self.wfile.write(line)
                                self.wfile.flush()
                                if (
                                    timeout is not None
                                    and timeout > 0
                                    and (time.time() - start) >= timeout
                                ):
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
                    items = _filter_k8s_items(items, label_sel, field_sel)
                    self._ok(
                        _list_with_rv(
                            items,
                            _to_deployment,
                            kind="Deployment",
                            api_version="apps/v1",
                            limit=limit if limit > 0 else None,
                            continue_token=cont,
                        )
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
            if d_plural == "replicasets":
                if d_name is None:
                    label_sel, field_sel = _selector_values_from_query(q)
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            rv = int(time.time() * 1000)
                            line = (
                                json.dumps(
                                    {
                                        "type": "BOOKMARK",
                                        "object": {
                                            "kind": "ReplicaSet",
                                            "apiVersion": "apps/v1",
                                            "metadata": {"resourceVersion": str(rv)},
                                        },
                                    },
                                    separators=(",", ":"),
                                ).encode("utf-8")
                                + b"\n"
                            )
                            self.wfile.write(line)
                            self.wfile.flush()
                        except BrokenPipeError:
                            pass
                        return
                    if not self._rbac_allows("list", "replicasets"):
                        self._deny(403)
                        return
                    deps = (
                        self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "deployments", d_ns)  # type: ignore[attr-defined]
                    )
                    rs_items = [_replicaset_from_deployment(dep) for dep in deps]
                    rs_items = _filter_k8s_items(rs_items, label_sel, field_sel)
                    self._ok(
                        _list_with_rv(
                            rs_items, _to_replicaset, kind="ReplicaSet", api_version="apps/v1"
                        )
                    )
                    return
                if not self._rbac_allows("get", "replicasets"):
                    self._deny(403)
                    return
                deps = (
                    self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                    if d_ns is None
                    else self.server.store.list("apps", "v1", "deployments", d_ns)  # type: ignore[attr-defined]
                )
                for dep in deps:
                    rs_obj = _replicaset_from_deployment(dep)
                    if rs_obj.name == d_name:
                        self._ok(_to_replicaset(rs_obj))
                        return
                self._not_found()
                return
            if d_plural == "statefulsets":
                transform = _to_statefulset
                if d_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "statefulsets"):
                            self._deny(403)
                            return
                        self._stream_watch(
                            "apps", "v1", "statefulsets", d_ns, q, transform=transform
                        )
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
                    self._ok(
                        _list_with_rv(
                            items,
                            transform,
                            kind="StatefulSet",
                            api_version="apps/v1",
                            limit=limit if limit > 0 else None,
                            continue_token=cont,
                        )
                    )
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
                        self._stream_watch(
                            "apps",
                            "v1",
                            "daemonsets",
                            d_ns,
                            q,
                            transform=lambda o: _to_daemonset(o),
                        )
                        return
                    if not self._rbac_allows("list", "daemonsets"):
                        self._deny(403)
                        return
                    items = (
                        self.server.store.list_all("apps", "v1", "daemonsets")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "daemonsets", d_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        _list_with_rv(
                            items,
                            lambda o: _to_daemonset(o),
                            kind="DaemonSet",
                            api_version="apps/v1",
                        )
                    )
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
                    label_sel, field_sel = _selector_values_from_query(q)
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        if not self._rbac_allows("watch", "ingresses"):
                            self._deny(403)
                            return
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            start = time.time()
                            timeout = int(q.get("timeoutSeconds", ["0"])[0] or 0) or None
                            heartbeat = int(q.get("heartbeatSeconds", ["0"])[0] or 0) or None
                            allow_bm = q.get("allowWatchBookmarks", ["0"])[0] in (
                                "1",
                                "true",
                                "True",
                            )
                            for ev_type, obj in self.server.store.watch(
                                "networking.k8s.io",
                                "v1",
                                "ingresses",
                                n_ns,
                                heartbeat_seconds=heartbeat,
                                allow_bookmarks=allow_bm,
                            ):  # type: ignore[attr-defined]
                                if ev_type != "BOOKMARK" and not _matches_selectors(
                                    obj, label_sel, field_sel
                                ):
                                    continue
                                line = (
                                    json.dumps(
                                        {
                                            "type": ev_type,
                                            "object": _to_ingress(
                                                obj, self.server.state, self.server.store
                                            ),
                                        },
                                        separators=(",", ":"),
                                    ).encode("utf-8")
                                    + b"\n"
                                )  # type: ignore[attr-defined]
                                self.wfile.write(line)
                                self.wfile.flush()
                                if (
                                    timeout is not None
                                    and timeout > 0
                                    and (time.time() - start) >= timeout
                                ):
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
                    items = _filter_k8s_items(items, label_sel, field_sel)
                    try:
                        limit = int(q.get("limit", ["0"])[0] or 0)
                    except Exception:
                        limit = 0
                    cont = q.get("continue", [""])[0] or None
                    self._ok(
                        _list_with_rv(
                            items,
                            lambda o: _to_ingress(o, self.server.state, self.server.store),
                            kind="Ingress",
                            api_version="networking.k8s.io/v1",
                            limit=limit if limit > 0 else None,
                            continue_token=cont,
                        )
                    )  # type: ignore[attr-defined]
                    return
                else:
                    if not self._rbac_allows("get", "ingresses"):
                        self._deny(403)
                        return
                    obj = self.server.store.get(
                        "networking.k8s.io", "v1", "ingresses", n_ns, n_name
                    )  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_ingress(obj, self.server.state, self.server.store))  # type: ignore[attr-defined]
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
                    self._ok(
                        _list_with_rv(items, transform, kind="CronJob", api_version="batch/v1")
                    )
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
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
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
                            eps = _endpointslice_for_service(
                                self.server.state, self.server.store, svc
                            )  # type: ignore[attr-defined]
                            if eps:
                                items.append(eps)
                        self._stream_fake_watch(
                            items, kind="EndpointSlice", api_version="discovery.k8s.io/v1"
                        )
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
                        eps = _endpointslice_for_service(self.server.state, self.server.store, svc)  # type: ignore[attr-defined]
                        if eps:
                            items.append(eps)
                    rv = max(
                        (int(i["metadata"].get("resourceVersion", "0")) for i in items), default=0
                    )
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
                    self._ok(
                        {
                            "kind": "EndpointSliceList",
                            "apiVersion": "discovery.k8s.io/v1",
                            "metadata": meta,
                            "items": selected,
                        }
                    )
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
                eps = _endpointslice_for_service(self.server.state, self.server.store, svc)  # type: ignore[attr-defined]
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
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
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

        # snapshot.storage.k8s.io: volumesnapshots (namespaced) and snapshot classes/contents
        if path.startswith("/apis/snapshot.storage.k8s.io/v1"):
            resources = (
                ("volumesnapshots", "VolumeSnapshot", "VolumeSnapshotList", True),
                (
                    "volumesnapshotclasses",
                    "VolumeSnapshotClass",
                    "VolumeSnapshotClassList",
                    False,
                ),
                (
                    "volumesnapshotcontents",
                    "VolumeSnapshotContent",
                    "VolumeSnapshotContentList",
                    False,
                ),
            )
            for plural, kind, list_kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(
                        path, "snapshot.storage.k8s.io", "v1", plural
                    )
                    if s_plural != plural:
                        continue
                    if s_name is None:
                        if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                            self._stream_watch(
                                "snapshot.storage.k8s.io",
                                "v1",
                                plural,
                                s_ns,
                                q,
                                transform=_to_generic(
                                    "snapshot.storage.k8s.io", "v1", kind, plural
                                ),
                            )
                            return
                        items = (
                            self.server.store.list_all("snapshot.storage.k8s.io", "v1", plural)
                            if s_ns is None
                            else self.server.store.list(
                                "snapshot.storage.k8s.io", "v1", plural, s_ns
                            )
                        )  # type: ignore[attr-defined]
                        self._ok(
                            {
                                "kind": list_kind,
                                "apiVersion": "snapshot.storage.k8s.io/v1",
                                "items": [
                                    _to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(i)
                                    for i in items
                                ],
                            }
                        )
                        return
                    obj = self.server.store.get(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io", "v1", plural, s_ns, s_name
                    )
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(obj))
                    return

                s_plural, s_name = _gv_cluster_name(path, "snapshot.storage.k8s.io", "v1", plural)
                if s_plural != plural:
                    continue
                if s_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "snapshot.storage.k8s.io",
                            "v1",
                            plural,
                            None,
                            q,
                            transform=_to_generic("snapshot.storage.k8s.io", "v1", kind, plural),
                        )
                        return
                    items = self.server.store.list_all(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io", "v1", plural
                    )
                    self._ok(
                        {
                            "kind": list_kind,
                            "apiVersion": "snapshot.storage.k8s.io/v1",
                            "items": [
                                _to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(  # type: ignore[attr-defined]
                    "snapshot.storage.k8s.io", "v1", plural, None, s_name
                )
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(obj))
                return

        # storage.k8s.io: storageclasses, volumeattachments, and CSI resources
        if path.startswith("/apis/storage.k8s.io/v1"):
            resources = (
                ("storageclasses", "StorageClass", "StorageClassList", False),
                ("volumeattachments", "VolumeAttachment", "VolumeAttachmentList", False),
                ("csidrivers", "CSIDriver", "CSIDriverList", False),
                ("csinodes", "CSINode", "CSINodeList", False),
                ("csistoragecapacities", "CSIStorageCapacity", "CSIStorageCapacityList", True),
            )
            for plural, kind, list_kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural:
                        continue
                    if s_name is None:
                        if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                            self._stream_watch(
                                "storage.k8s.io",
                                "v1",
                                plural,
                                s_ns,
                                q,
                                transform=_to_generic("storage.k8s.io", "v1", kind, plural),
                            )
                            return
                        items = (
                            self.server.store.list_all("storage.k8s.io", "v1", plural)  # type: ignore[attr-defined]
                            if s_ns is None
                            else self.server.store.list("storage.k8s.io", "v1", plural, s_ns)  # type: ignore[attr-defined]
                        )
                        self._ok(
                            {
                                "kind": list_kind,
                                "apiVersion": "storage.k8s.io/v1",
                                "items": [
                                    _to_generic("storage.k8s.io", "v1", kind, plural)(i)
                                    for i in items
                                ],
                            }
                        )
                        return
                    obj = self.server.store.get(  # type: ignore[attr-defined]
                        "storage.k8s.io", "v1", plural, s_ns, s_name
                    )
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_generic("storage.k8s.io", "v1", kind, plural)(obj))
                    return

                s_plural, s_name = _gv_cluster_name(path, "storage.k8s.io", "v1", plural)
                if s_plural != plural:
                    continue
                if s_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "storage.k8s.io",
                            "v1",
                            plural,
                            None,
                            q,
                            transform=_to_generic("storage.k8s.io", "v1", kind, plural),
                        )
                        return
                    items = self.server.store.list_all(  # type: ignore[attr-defined]
                        "storage.k8s.io", "v1", plural
                    )
                    self._ok(
                        {
                            "kind": list_kind,
                            "apiVersion": "storage.k8s.io/v1",
                            "items": [
                                _to_generic("storage.k8s.io", "v1", kind, plural)(i) for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(  # type: ignore[attr-defined]
                    "storage.k8s.io", "v1", plural, None, s_name
                )
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("storage.k8s.io", "v1", kind, plural)(obj))
                return

        # rbac: roles/rolebindings (namespaced) and clusterroles/clusterrolebindings (cluster-scoped)
        if path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced
            r_plural, r_ns, r_name = _gv_ns_name(path, "rbac.authorization.k8s.io", "v1", "roles")
            if r_plural == "roles":
                if r_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "rbac.authorization.k8s.io",
                            "v1",
                            "roles",
                            r_ns,
                            q,
                            transform=_to_generic(
                                "rbac.authorization.k8s.io", "v1", "Role", "roles"
                            ),
                        )
                        return
                    items = (
                        self.server.store.list_all("rbac.authorization.k8s.io", "v1", "roles")  # type: ignore[attr-defined]
                        if r_ns is None
                        else self.server.store.list(
                            "rbac.authorization.k8s.io", "v1", "roles", r_ns
                        )  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "RoleList",
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "items": [
                                _to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(
                    "rbac.authorization.k8s.io", "v1", "roles", r_ns, r_name
                )  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("rbac.authorization.k8s.io", "v1", "Role", "roles")(obj))
                return
            rb_plural, rb_ns, rb_name = _gv_ns_name(
                path, "rbac.authorization.k8s.io", "v1", "rolebindings"
            )
            if rb_plural == "rolebindings":
                if rb_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "rbac.authorization.k8s.io",
                            "v1",
                            "rolebindings",
                            rb_ns,
                            q,
                            transform=_to_generic(
                                "rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings"
                            ),
                        )
                        return
                    items = (
                        self.server.store.list_all(
                            "rbac.authorization.k8s.io", "v1", "rolebindings"
                        )  # type: ignore[attr-defined]
                        if rb_ns is None
                        else self.server.store.list(
                            "rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns
                        )  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "RoleBindingList",
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "items": [
                                _to_generic(
                                    "rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings"
                                )(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(
                    "rbac.authorization.k8s.io", "v1", "rolebindings", rb_ns, rb_name
                )  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(
                    _to_generic("rbac.authorization.k8s.io", "v1", "RoleBinding", "rolebindings")(
                        obj
                    )
                )
                return
            # cluster-scoped
            cr_plural, cr_name = _gv_cluster_name(
                path, "rbac.authorization.k8s.io", "v1", "clusterroles"
            )
            if cr_plural == "clusterroles":
                if cr_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "rbac.authorization.k8s.io",
                            "v1",
                            "clusterroles",
                            None,
                            q,
                            transform=_to_generic(
                                "rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles"
                            ),
                        )
                        return
                    items = self.server.store.list_all(
                        "rbac.authorization.k8s.io", "v1", "clusterroles"
                    )  # type: ignore[attr-defined]
                    self._ok(
                        {
                            "kind": "ClusterRoleList",
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "items": [
                                _to_generic(
                                    "rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles"
                                )(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(
                    "rbac.authorization.k8s.io", "v1", "clusterroles", None, cr_name
                )  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(
                    _to_generic("rbac.authorization.k8s.io", "v1", "ClusterRole", "clusterroles")(
                        obj
                    )
                )
                return
            crb_plural, crb_name = _gv_cluster_name(
                path, "rbac.authorization.k8s.io", "v1", "clusterrolebindings"
            )
            if crb_plural == "clusterrolebindings":
                if crb_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "rbac.authorization.k8s.io",
                            "v1",
                            "clusterrolebindings",
                            None,
                            q,
                            transform=_to_generic(
                                "rbac.authorization.k8s.io",
                                "v1",
                                "ClusterRoleBinding",
                                "clusterrolebindings",
                            ),
                        )
                        return
                    items = self.server.store.list_all(
                        "rbac.authorization.k8s.io", "v1", "clusterrolebindings"
                    )  # type: ignore[attr-defined]
                    self._ok(
                        {
                            "kind": "ClusterRoleBindingList",
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "items": [
                                _to_generic(
                                    "rbac.authorization.k8s.io",
                                    "v1",
                                    "ClusterRoleBinding",
                                    "clusterrolebindings",
                                )(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get(
                    "rbac.authorization.k8s.io", "v1", "clusterrolebindings", None, crb_name
                )  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(
                    _to_generic(
                        "rbac.authorization.k8s.io",
                        "v1",
                        "ClusterRoleBinding",
                        "clusterrolebindings",
                    )(obj)
                )
                return

        # policy/v1 PodDisruptionBudget
        if path.startswith("/apis/policy/v1"):
            p_plural, p_ns, p_name = _gv_ns_name(path, "policy", "v1", "poddisruptionbudgets")
            if p_plural == "poddisruptionbudgets":
                if p_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "policy",
                            "v1",
                            "poddisruptionbudgets",
                            p_ns,
                            q,
                            transform=_to_generic(
                                "policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets"
                            ),
                        )
                        return
                    items = (
                        self.server.store.list_all("policy", "v1", "poddisruptionbudgets")  # type: ignore[attr-defined]
                        if p_ns is None
                        else self.server.store.list("policy", "v1", "poddisruptionbudgets", p_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "PodDisruptionBudgetList",
                            "apiVersion": "policy/v1",
                            "items": [
                                _to_generic(
                                    "policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets"
                                )(i)
                                for i in items
                            ],
                        }
                    )
                    return
                obj = self.server.store.get("policy", "v1", "poddisruptionbudgets", p_ns, p_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(
                    _to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(obj)
                )
                return

        # autoscaling/v2 HPA
        if path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(
                path, "autoscaling", "v2", "horizontalpodautoscalers"
            )
            if h_plural == "horizontalpodautoscalers":
                if h_name is None:
                    if q.get("watch", ["0"])[0] in ("1", "true", "True"):
                        self._stream_watch(
                            "autoscaling",
                            "v2",
                            "horizontalpodautoscalers",
                            h_ns,
                            q,
                            transform=lambda o: _to_hpa(o, self.server.store),
                        )  # type: ignore[attr-defined]
                        return
                    items = (
                        self.server.store.list_all("autoscaling", "v2", "horizontalpodautoscalers")  # type: ignore[attr-defined]
                        if h_ns is None
                        else self.server.store.list(
                            "autoscaling", "v2", "horizontalpodautoscalers", h_ns
                        )  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "HorizontalPodAutoscalerList",
                            "apiVersion": "autoscaling/v2",
                            "items": [_to_hpa(i, self.server.store) for i in items],
                        }
                    )  # type: ignore[attr-defined]
                    return
                obj = self.server.store.get(
                    "autoscaling", "v2", "horizontalpodautoscalers", h_ns, h_name
                )  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_hpa(obj, self.server.store))  # type: ignore[attr-defined]
                return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self.path = path
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if path == "/api/v1/sessiontokens":
            if not self._authz(role="mint"):
                return
            doc = _read_json(self._read_body())
            role = str(doc.get("role") or "exec").strip().lower()
            scopes_val = doc.get("scopes")
            if scopes_val is None:
                scopes_val = doc.get("scope")
            scopes: list[str]
            if isinstance(scopes_val, str):
                scopes = [scopes_val]
            elif isinstance(scopes_val, list | tuple):
                scopes = [str(s) for s in scopes_val if s]
            else:
                scopes = []
            try:
                ttl_req = int(doc.get("ttlSeconds") or doc.get("ttl") or 0)
            except Exception:
                ttl_req = 0
            try:
                minted = self._mint_session_token(role=role, scopes=scopes, ttl_seconds=ttl_req)
            except ValueError as exc:
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message=str(exc),
                )
                return
            except RuntimeError as exc:
                self._json_status(
                    HTTPStatus.NOT_FOUND,
                    reason="NotFound",
                    message=str(exc),
                )
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._json_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    reason="InternalError",
                    message=str(exc),
                )
                return
            self._ok(minted)
            return
        is_exec_path = re.match(r"^/api/v1/namespaces/[^/]+/pods/[^/]+/exec$", path)
        is_pf_path = re.match(r"^/api/v1/namespaces/[^/]+/(pods|services)/[^/]+/portforward$", path)
        # SubjectAccessReview should be callable by read tokens; other POSTs require write/admin.
        if path.startswith("/apis/authorization.k8s.io/"):
            if not self._authz(role="read"):
                return
        else:
            if is_exec_path and upgrade:
                if not self._authz(role="exec"):
                    return
            elif is_pf_path and upgrade:
                if not self._authz(role="portforward"):
                    return
            else:
                if not self._authz(role="write"):
                    return
        if self._reject_ha_workload_mutation("POST", path):
            return

        # Pod exec (kubectl uses POST + SPDY upgrade)
        m_exec_spdy = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/exec$", path)
        if m_exec_spdy:
            if not self._rbac_allows("create", "pods/exec"):
                self._deny(403)
                return
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade:
                qs = parse_qs(parsed.query)
                cmd = qs.get("command") or []
                if not cmd:
                    self._json_status(
                        HTTPStatus.BAD_REQUEST,
                        reason="BadRequest",
                        message="command query param is required",
                    )
                    return
                expected_uid = self._extract_pod_uid(qs)
                expected_rv = self._extract_pod_rv(qs)
                container_info = self._validate_pod_scope(
                    namespace=m_exec_spdy.group(1),
                    pod_name=m_exec_spdy.group(2),
                    scope_env="AE_API_EXEC_SCOPE",
                    action="exec",
                    expected_uid=expected_uid,
                    expected_rv=expected_rv,
                )
                if container_info is None:
                    return
                exec_runtime, _node_id, _endpoint = self._runtime_for_pod(
                    m_exec_spdy.group(1), m_exec_spdy.group(2), container_info=container_info
                )
                if upgrade.startswith("spdy"):
                    container = (qs.get("container") or [None])[0]
                    tty = (qs.get("tty") or ["false"])[0].lower() in ("1", "true", "yes")
                    want_stdin = (qs.get("stdin") or ["false"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    want_stdout = (qs.get("stdout") or ["true"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    want_stderr = (qs.get("stderr") or ["true"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    self._handle_exec_spdy(
                        namespace=m_exec_spdy.group(1),
                        pod_name=m_exec_spdy.group(2),
                        command=list(cmd),
                        container=container,
                        tty=tty,
                        want_stdin=want_stdin,
                        want_stdout=want_stdout,
                        want_stderr=want_stderr,
                        runtime=exec_runtime,
                    )
                elif upgrade == "websocket":
                    container = (qs.get("container") or [None])[0]
                    tty = (qs.get("tty") or ["false"])[0].lower() in ("1", "true", "yes")
                    want_stdin = (qs.get("stdin") or ["false"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    want_stdout = (qs.get("stdout") or ["true"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    want_stderr = (qs.get("stderr") or ["true"])[0].lower() in (
                        "1",
                        "true",
                        "yes",
                    )
                    self._handle_exec_ws(
                        namespace=m_exec_spdy.group(1),
                        pod_name=m_exec_spdy.group(2),
                        command=list(cmd),
                        container=container,
                        tty=tty,
                        want_stdin=want_stdin,
                        want_stdout=want_stdout,
                        want_stderr=want_stderr,
                        runtime=exec_runtime,
                    )
                else:
                    self._json_status(
                        HTTPStatus.UPGRADE_REQUIRED,
                        reason="UpgradeRequired",
                        message="exec requires SPDY/3.1 upgrade used by kubectl",
                    )
                return
        body = self._read_body()
        doc = _read_json(body)

        if path.startswith("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"):
            status = self._eval_subject_access_review(doc.get("spec") or {})
            resp = {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
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

        if path.startswith("/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"):
            principal = self._parse_principal()
            token_role = principal.token_role or ""
            if not self.rbac_enabled or token_role == "admin":
                resource_rules = [{"verbs": ["*"], "apiGroups": ["*"], "resources": ["*"]}]
                non_resource_rules = [{"verbs": ["*"], "nonResourceURLs": ["*"]}]
                incomplete = False
            elif token_role == "read":
                resource_rules = [
                    {
                        "verbs": ["get", "list", "watch"],
                        "apiGroups": ["*"],
                        "resources": ["*"],
                    }
                ]
                non_resource_rules = [{"verbs": ["get", "list", "watch"], "nonResourceURLs": ["*"]}]
                incomplete = False
            else:
                resource_rules = []
                non_resource_rules = []
                incomplete = True
            resp = {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectRulesReview",
                "spec": doc.get("spec") or {},
                "status": {
                    "resourceRules": resource_rules,
                    "nonResourceRules": non_resource_rules,
                    "incomplete": incomplete,
                },
            }
            out = _json(resp)
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

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
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="command must be a non-empty list",
                )
                return
            qs = parse_qs(parsed.query)
            expected_uid = self._extract_pod_uid(qs)
            expected_rv = self._extract_pod_rv(qs)
            container_info = self._validate_pod_scope(
                namespace=m_exec.group(1),
                pod_name=m_exec.group(2),
                scope_env="AE_API_EXEC_SCOPE",
                action="exec",
                expected_uid=expected_uid,
                expected_rv=expected_rv,
            )
            if container_info is None:
                return
            exec_runtime, _node_id, _endpoint = self._runtime_for_pod(
                m_exec.group(1), m_exec.group(2), container_info=container_info
            )
            try:
                rc = int(
                    exec_runtime.exec(  # type: ignore[attr-defined]
                        m_exec.group(2),
                        [str(c) for c in cmd],
                        timeout=int(timeout) if timeout else None,
                    )
                )
                self._ok(
                    {
                        "kind": "Status",
                        "status": "Success",
                        "code": 200,
                        "metadata": {},
                        "details": {"exitCode": rc},
                    }
                )
            except Exception as exc:
                self._json_status(
                    HTTPStatus.INTERNAL_SERVER_ERROR, reason="InternalError", message=str(exc)
                )
            return
        # Pod port-forward (kubectl uses POST + SPDY upgrade)
        m_pf = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)/portforward$", path)
        if m_pf:
            if not self._rbac_allows("create", "pods/portforward"):
                self._deny(403)
                return
            qs = parse_qs(parsed.query)
            ports_q = qs.get("ports") or []
            ns = m_pf.group(1)
            pod_name = m_pf.group(2)
            container_info = self._validate_pod_scope(
                namespace=ns,
                pod_name=pod_name,
                scope_env="AE_API_PF_SCOPE",
                action="port-forward",
                expected_uid=self._extract_pod_uid(qs),
                expected_rv=self._extract_pod_rv(qs),
            )
            if container_info is None:
                return
            pf_runtime, _node_id, _endpoint = self._runtime_for_pod(
                ns, pod_name, container_info=container_info
            )
            requested_ports: list[int] = []
            for p in ports_q:
                try:
                    requested_ports.append(int(p))
                except Exception:
                    pass
            target_ports = list(requested_ports)
            if not target_ports:
                # Allow stream headers to select the port for pod port-forward
                target_ports = [0]
            if isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                try:
                    target_ports = [int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))]
                except Exception:
                    target_ports = [8081]
            if not target_ports:
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="ports query param required",
                )
                return
            target_host = "127.0.0.1"
            port_map: dict[int, int] = {}
            pod_id = None
            pod_ip = None
            use_host_ports = False
            if container_info:
                host_ip = container_info.get("host_ip") or container_info.get("hostIP")
                pod_ip = container_info.get("pod_ip")
                pod_id = container_info.get("uid") or container_info.get("id")
                target_host = pod_ip or host_ip or target_host
                if not pod_ip:
                    port_map = container_info.get("port_map") or {}
                    use_host_ports = bool(port_map)
                if target_host in ("0.0.0.0", "::", ""):  # noqa: S104
                    target_host = "127.0.0.1"
            elif isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_host = os.getenv("AE_STUB_BACKEND_HOST", target_host)
            use_agent_pf = (
                pf_runtime is not self.server.runtime  # type: ignore[attr-defined]
                and hasattr(pf_runtime, "port_forward_socket")
            )
            if use_agent_pf and requested_ports:
                target_ports = list(requested_ports)
            upstream_factory = None
            if use_agent_pf:

                def _pf_open(port: int, _rt=pf_runtime) -> socket.socket | None:
                    try:
                        return _rt.port_forward_socket(  # type: ignore[attr-defined]
                            pod_id=str(pod_id) if pod_id else None,
                            pod_name=str(pod_name) if pod_name else None,
                            namespace=ns,
                            port=int(port),
                        )
                    except Exception:
                        return None

                upstream_factory = _pf_open
            pf_port_map: dict[int, int] | None = None
            cri_pf_procs: list[subprocess.Popen] = []
            use_cri_pf = (
                isinstance(self.server.runtime, CRIRuntime)  # type: ignore[attr-defined]
                and pod_id
                and (self._cri_pf_force() or (self._cri_pf_enabled() and not pod_ip))
            )
            if use_cri_pf and requested_ports:
                pf_port_map, cri_pf_procs = self._start_cri_port_forward(
                    str(pod_id), requested_ports
                )
                if pf_port_map:
                    target_host = "127.0.0.1"
                    target_ports = list(requested_ports)
                    use_host_ports = False
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                pf_ports = (
                    target_ports if not use_host_ports else list(port_map.keys()) or target_ports
                )
                if pf_port_map:
                    pf_ports = list(requested_ports or target_ports)
                port_label = ",".join(str(p) for p in pf_ports)
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    pod=pod_name,
                    ports=port_label,
                    protocol="spdy",
                )
                try:
                    self._handle_port_forward_spdy(
                        target_host,
                        pf_ports,
                        port_map=pf_port_map or (port_map if use_host_ports else None),
                        upstream_factory=upstream_factory,
                    )
                finally:
                    if cri_pf_procs:
                        self._stop_cri_port_forward(cri_pf_procs)
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        pod=pod_name,
                        ports=port_label,
                    )
            elif upgrade == "websocket":
                port_label = ",".join(str(p) for p in (requested_ports or target_ports))
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    pod=pod_name,
                    ports=port_label,
                    protocol="websocket",
                )
                try:
                    ws_port = target_ports[0]
                    if pf_port_map:
                        ws_port = pf_port_map.get(target_ports[0], ws_port)
                    self._handle_port_forward_ws(
                        target_host, ws_port, upstream_factory=upstream_factory
                    )
                finally:
                    if cri_pf_procs:
                        self._stop_cri_port_forward(cri_pf_procs)
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        pod=pod_name,
                        ports=port_label,
                    )
            else:
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="port-forward requires SPDY/3.1 used by kubectl",
                )
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
            if not self._validate_service_pf_scope(ns, svc, svc_name):
                return
            ep = _endpoints_for_service(self.server.state, self.server.store, svc)  # type: ignore[attr-defined]
            subsets = (ep or {}).get("subsets") or []
            addresses = subsets[0].get("addresses") if subsets else None
            target_ip = (addresses[0].get("ip") if addresses else None) if addresses else None
            if not target_ip and subsets:
                nr = subsets[0].get("notReadyAddresses") or []
                target_ip = (nr[0].get("ip") if nr else None) if nr else None
            if not target_ip and isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                target_ip = os.getenv("AE_STUB_BACKEND_HOST", "127.0.0.1")
            if not target_ip:
                self._json_status(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    reason="NoEndpoints",
                    message="no ready endpoints for service",
                )
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
            if isinstance(self.server.runtime, StubRuntime):  # type: ignore[attr-defined]
                try:
                    target_ports = [int(os.getenv("AE_STUB_BACKEND_PORT", "8081"))]
                except Exception:
                    target_ports = [8081]
            if not target_ports:
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="ports query param required",
                )
                return
            upstream_factory = None
            try:
                node_rec = self._node_record_for_ip(str(target_ip))
            except Exception:
                node_rec = None
            if node_rec and getattr(node_rec, "endpoint", None):
                svc_runtime = self._runtime_for_endpoint(getattr(node_rec, "endpoint", None))
                if hasattr(svc_runtime, "port_forward_socket"):
                    container = self._container_for_pod_ip(svc_runtime, ns, str(target_ip))
                    if container:
                        pod_id = container.get("uid") or container.get("id")
                        labels = container.get("labels", {}) or {}
                        pod_name = (
                            labels.get("ae.pod_name")
                            or labels.get("ae.replica_id")
                            or container.get("name")
                        )

                        # Use node agent port-forward when available.

                        def _pf_open(port: int, _rt=svc_runtime) -> socket.socket | None:
                            try:
                                return _rt.port_forward_socket(  # type: ignore[attr-defined]
                                    pod_id=str(pod_id) if pod_id else None,
                                    pod_name=str(pod_name) if pod_name else None,
                                    namespace=ns,
                                    port=int(port),
                                )
                            except Exception:
                                return None

                        upstream_factory = _pf_open
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if upgrade.startswith("spdy"):
                port_label = ",".join(str(p) for p in target_ports)
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    service=svc_name,
                    ports=port_label,
                    protocol="spdy",
                )
                try:
                    self._handle_port_forward_spdy(
                        target_ip, target_ports, upstream_factory=upstream_factory
                    )
                finally:
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        service=svc_name,
                        ports=port_label,
                    )
            elif upgrade == "websocket":
                port_label = ",".join(str(p) for p in target_ports)
                self._audit(
                    "portforward.start",
                    namespace=ns,
                    service=svc_name,
                    ports=port_label,
                    protocol="websocket",
                )
                try:
                    self._handle_port_forward_ws(
                        target_ip, target_ports[0], upstream_factory=upstream_factory
                    )
                finally:
                    self._audit(
                        "portforward.end",
                        namespace=ns,
                        service=svc_name,
                        ports=port_label,
                    )
            else:
                self._json_status(
                    HTTPStatus.UPGRADE_REQUIRED,
                    reason="UpgradeRequired",
                    message="port-forward requires SPDY/3.1 used by kubectl",
                )
            return

        plural, ns, name = _ns_name(path)
        if plural in {
            "namespaces",
            "configmaps",
            "secrets",
            "persistentvolumeclaims",
            "persistentvolumes",
            "serviceaccounts",
            "services",
        }:
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            if not isinstance(name_in, str) or not name_in:
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="metadata.name required",
                )
                return
            ns_in = md.get("namespace") or ns
            if plural in {"namespaces", "persistentvolumes"}:
                ns_in = None
            if plural == "secrets":
                _set_secret_type(md, doc.get("type"))
            if not _valid_name(name_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.name (DNS-1123 label)",
                )
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.namespace (DNS-1123 label)",
                )
                return
            spec_in = (
                doc.get("data")
                if plural in {"configmaps", "secrets"}
                else (
                    _service_account_spec_payload(doc)
                    if plural == "serviceaccounts"
                    else (doc.get("spec") or {})
                )
            )
            status_in = doc.get("status") or {}
            # Service enrichments: allocate clusterIP/nodePort if missing and validate collisions
            if plural == "serviceaccounts":
                annotations = md.setdefault("annotations", {})
                token = self._issue_sa_token(ns_in or "default", name_in)
                annotations.setdefault("ae.apishim/token", token)
                annotations.setdefault(
                    "ae.apishim/token-exp", str(int(time.time() + self.sa_token_ttl))
                )
            if plural == "services":
                spec_in = dict(spec_in or {})
                existing_svcs = self.server.store.list_all("", "v1", "services")  # type: ignore[attr-defined]
                existing_cluster_ips = {
                    s.spec.get("clusterIP") for s in existing_svcs if s.spec.get("clusterIP")
                }
                # include provider allocations to avoid clashes across restart
                try:
                    existing_cluster_ips |= {
                        s.cluster_ip for s in self.server.state.list_services()
                    }  # type: ignore[attr-defined]
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
                cluster_ip = spec_in.get("clusterIP")
                if svc_type != "ExternalName" and cluster_ip != "None":
                    if not cluster_ip:
                        spec_in["clusterIP"] = _alloc_cluster_ip(
                            ns_in, name_in, existing_cluster_ips
                        )
                    else:
                        cip = str(cluster_ip)
                        if cip in existing_cluster_ips:
                            self._json_status(
                                HTTPStatus.CONFLICT,
                                reason="AlreadyExists",
                                message=f"clusterIP {cip} already allocated",
                            )
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
                                self._json_status(
                                    HTTPStatus.UNPROCESSABLE_ENTITY,
                                    reason="Invalid",
                                    message="nodePort must be integer",
                                )
                                return
                            if np_i in existing_nodeports:
                                self._json_status(
                                    HTTPStatus.CONFLICT,
                                    reason="AlreadyExists",
                                    message=f"nodePort {np_i} already allocated",
                                )
                                return
                            existing_nodeports.add(np_i)
                            p["nodePort"] = np_i
                        else:
                            np_alloc = _alloc_nodeport(
                                existing_nodeports,
                                f"{ns_in}/{name_in}/{p.get('name')}/{p.get('port')}",
                            )
                            p["nodePort"] = np_alloc
                            existing_nodeports.add(np_alloc)
                        new_ports.append(p)
                    spec_in["ports"] = new_ports
                status_in = status_in or {}
                if svc_type in {"LoadBalancer", "NodePort"}:
                    if "status" not in status_in or not status_in:
                        status_in = {"loadBalancer": {"ingress": []}}
                status_in = _service_lb_status(
                    spec_in, status_in, None
                )  # provider IP may not exist yet during create
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.namespace (DNS-1123 label)",
                    )
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
                    status=_synthesize_deploy_status(
                        doc.get("spec") or {}, doc.get("status") or {}
                    ),
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata",
                    )
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
                    status=_synthesize_deploy_status(
                        doc.get("spec") or {}, doc.get("status") or {}
                    ),
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata",
                    )
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                if not ns_in or not _valid_name(ns_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.namespace (DNS-1123 label)",
                    )
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
                out = _json(_to_ingress(created, self.server.state, self.server.store))
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata",
                    )
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

        # snapshot.storage.k8s.io resources
        if self.path.startswith("/apis/snapshot.storage.k8s.io/v1"):
            resources = (
                ("volumesnapshots", "VolumeSnapshot", True),
                ("volumesnapshotclasses", "VolumeSnapshotClass", False),
                ("volumesnapshotcontents", "VolumeSnapshotContent", False),
            )
            for plural, kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(
                        self.path, "snapshot.storage.k8s.io", "v1", plural
                    )
                    if s_plural != plural or s_name:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name")
                    ns_in = md.get("namespace") or s_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=doc.get("spec") or {},
                        status=doc.get("status") or {},
                    )
                    self.send_response(HTTPStatus.CREATED)
                    out = _json(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(created))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return

                s_plural, s_name = _gv_cluster_name(
                    self.path, "snapshot.storage.k8s.io", "v1", plural
                )
                if s_plural != plural or s_name:
                    continue
                md = doc.get("metadata") or {}
                name_in = md.get("name")
                if not name_in or not _valid_name(name_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "snapshot.storage.k8s.io",
                    "v1",
                    plural,
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, plural),
                    spec=_spec_payload(plural, doc),
                    status=doc.get("status") or {},
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # storage.k8s.io resources
        if self.path.startswith("/apis/storage.k8s.io/v1"):
            resources = (
                ("storageclasses", "StorageClass", False),
                ("volumeattachments", "VolumeAttachment", False),
                ("csidrivers", "CSIDriver", False),
                ("csinodes", "CSINode", False),
                ("csistoragecapacities", "CSIStorageCapacity", True),
            )
            for plural, kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(self.path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or s_name
                    ns_in = md.get("namespace") or s_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "storage.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                else:
                    s_plural, s_name = _gv_cluster_name(self.path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or s_name
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    created = self.server.store.upsert(  # type: ignore[attr-defined]
                        "storage.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_generic("storage.k8s.io", "v1", kind, plural)(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # rbac (namespaced and cluster resources)
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            # namespaced roles/rolebindings
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(
                    self.path, "rbac.authorization.k8s.io", "v1", plural
                )
                if r_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or r_name
                    ns_in = md.get("namespace") or r_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
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
                    out = _json(
                        _to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created)
                    )
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
            # clusterroles/clusterrolebindings
            for plural, kind in (
                ("clusterroles", "ClusterRole"),
                ("clusterrolebindings", "ClusterRoleBinding"),
            ):
                cr_plural, cr_name = _gv_cluster_name(
                    self.path, "rbac.authorization.k8s.io", "v1", plural
                )
                if cr_plural == plural:
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or cr_name
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
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
                    out = _json(
                        _to_generic("rbac.authorization.k8s.io", "v1", kind, plural)(created)
                    )
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata",
                    )
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
                out = _json(
                    _to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(
                        created
                    )
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        # autoscaling/v2 HPA
        if path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(
                path, "autoscaling", "v2", "horizontalpodautoscalers"
            )
            if h_plural == "horizontalpodautoscalers":
                md = doc.get("metadata") or {}
                name_in = md.get("name") or h_name
                ns_in = md.get("namespace") or h_ns
                if not name_in or not _valid_name(name_in) or not ns_in or not _valid_name(ns_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata",
                    )
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
                out = _json(
                    _to_generic(
                        "autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers"
                    )(created)
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

        self._not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authz():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        self.path = path
        if self._reject_ha_workload_mutation("PUT", path):
            return
        body = self._read_body()
        doc = _read_json(body)
        plural, ns, name = _ns_name(self.path)
        if (
            plural
            in {
                "namespaces",
                "configmaps",
                "secrets",
                "persistentvolumeclaims",
                "persistentvolumes",
                "serviceaccounts",
                "services",
            }
            and name
        ):
            md = doc.get("metadata") or {}
            name_in = md.get("name") or name
            ns_in = md.get("namespace") or ns
            if plural in {"namespaces", "persistentvolumes"}:
                ns_in = None
            if plural == "secrets":
                _set_secret_type(md, doc.get("type"))
            if not _valid_name(name_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.name (DNS-1123 label)",
                )
                return
            if ns_in is not None and not _valid_name(ns_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.namespace (DNS-1123 label)",
                )
                return
            updated = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=(
                    doc.get("data")
                    if plural in {"configmaps", "secrets"}
                    else (
                        _service_account_spec_payload(doc)
                        if plural == "serviceaccounts"
                        else (doc.get("spec") or {})
                    )
                ),
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
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                if ns_in is not None and not _valid_name(ns_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.namespace (DNS-1123 label)",
                    )
                    return
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(
                        doc.get("spec") or {}, doc.get("status") or {}
                    ),
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
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="spec.replicas must be >= 0",
                )
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
                    status=_synthesize_deploy_status(
                        doc.get("spec") or {}, doc.get("status") or {}
                    ),
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
                self._ok(_to_ingress(updated, self.server.state, self.server.store))
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
        if self.path.startswith("/apis/storage.k8s.io/v1"):
            resources = (
                ("storageclasses", "StorageClass", False),
                ("volumeattachments", "VolumeAttachment", False),
                ("csidrivers", "CSIDriver", False),
                ("csinodes", "CSINode", False),
                ("csistoragecapacities", "CSIStorageCapacity", True),
            )
            for plural, kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(self.path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural or not s_name:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or s_name
                    ns_in = md.get("namespace") or s_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
                        return
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "storage.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                else:
                    s_plural, s_name = _gv_cluster_name(self.path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural or not s_name:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or s_name
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "storage.k8s.io",
                        "v1",
                        plural,
                        None,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, None, plural),
                        spec=_spec_payload(plural, doc),
                        status=doc.get("status") or {},
                    )
                self._ok(_to_generic("storage.k8s.io", "v1", kind, plural)(updated))
                return
        if self.path.startswith("/apis/snapshot.storage.k8s.io/v1"):
            resources = (
                ("volumesnapshots", "VolumeSnapshot", True),
                ("volumesnapshotclasses", "VolumeSnapshotClass", False),
                ("volumesnapshotcontents", "VolumeSnapshotContent", False),
            )
            for plural, kind, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(
                        self.path, "snapshot.storage.k8s.io", "v1", plural
                    )
                    if s_plural != plural or not s_name:
                        continue
                    md = doc.get("metadata") or {}
                    name_in = md.get("name") or s_name
                    ns_in = md.get("namespace") or s_ns
                    if not name_in or not _valid_name(name_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return
                    if not ns_in or not _valid_name(ns_in):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
                        return
                    updated = self.server.store.upsert(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io",
                        "v1",
                        plural,
                        ns_in,
                        name_in,
                        metadata=_normalize_metadata(md, name_in, ns_in, plural),
                        spec=doc.get("spec") or {},
                        status=doc.get("status") or {},
                    )
                    self._ok(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(updated))
                    return

                s_plural, s_name = _gv_cluster_name(
                    self.path, "snapshot.storage.k8s.io", "v1", plural
                )
                if s_plural != plural or not s_name:
                    continue
                md = doc.get("metadata") or {}
                name_in = md.get("name") or s_name
                if not name_in or not _valid_name(name_in):
                    self._json_status(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        reason="Invalid",
                        message="invalid metadata.name (DNS-1123 label)",
                    )
                    return
                updated = self.server.store.upsert(  # type: ignore[attr-defined]
                    "snapshot.storage.k8s.io",
                    "v1",
                    plural,
                    None,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, None, plural),
                    spec=_spec_payload(plural, doc),
                    status=doc.get("status") or {},
                )
                self._ok(_to_generic("snapshot.storage.k8s.io", "v1", kind, plural)(updated))
                return
        if self.path.startswith("/apis/rbac.authorization.k8s.io/v1"):
            for plural, kind in (("roles", "Role"), ("rolebindings", "RoleBinding")):
                r_plural, r_ns, r_name = _gv_ns_name(
                    self.path, "rbac.authorization.k8s.io", "v1", plural
                )
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
            for plural, kind in (
                ("clusterroles", "ClusterRole"),
                ("clusterrolebindings", "ClusterRoleBinding"),
            ):
                cr_plural, cr_name = _gv_cluster_name(
                    self.path, "rbac.authorization.k8s.io", "v1", plural
                )
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
                self._ok(
                    _to_generic("policy", "v1", "PodDisruptionBudget", "poddisruptionbudgets")(
                        updated
                    )
                )
                return
        # autoscaling/v2 HPA
        if self.path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(
                self.path, "autoscaling", "v2", "horizontalpodautoscalers"
            )
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
        if self._reject_ha_workload_mutation("PATCH", path):
            return
        q = parse_qs(parsed.query)
        field_manager = q.get("fieldManager", ["kubectl"])[0] or "kubectl"
        force_flag = (q.get("force", ["false"])[0] or "").lower() in {"1", "true", "yes"}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        body = self._read_body()
        patch = _read_json(body)
        patch_debug = os.getenv("AE_APISHIM_PATCH_DEBUG", "0") == "1"
        plural, ns, name = _ns_name(path)
        if (
            plural
            in {
                "namespaces",
                "configmaps",
                "secrets",
                "persistentvolumeclaims",
                "persistentvolumes",
                "serviceaccounts",
                "services",
            }
            and name
        ):
            obj = self.server.store.get(
                "", "v1", plural, None if plural == "namespaces" else ns, name
            )  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return
            base = _to_obj(obj)
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return
            md = merged.get("metadata") or {}
            if plural == "secrets":
                _set_secret_type(md, merged.get("type"))
            patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
            if patch_debug:
                LOGGER.info(
                    "patch %s/%s ctype=%s manager=%s paths=%s",
                    plural,
                    name,
                    ctype or "<empty>",
                    field_manager,
                    sorted(patch_paths),
                )
            if ctype.startswith("application/apply-patch") and _managed_conflict(
                obj.metadata, field_manager, patch_paths, force_flag
            ):
                self._json_status(
                    HTTPStatus.CONFLICT,
                    reason="Conflict",
                    message="managedFields conflict on apply",
                )
                return
            if ctype.startswith("application/apply-patch"):
                if not patch_paths:
                    patch_paths = {"*"}
                md = _merge_dict(dict(obj.metadata), md)
                md = _update_managed_fields(
                    md, "v1", field_manager, "Apply", fields=patch_paths, force=force_flag
                )
                md = _ensure_managed_fields_entry(md, "v1", field_manager, "Apply", patch_paths)
            elif ctype in (
                "application/merge-patch+json",
                "application/strategic-merge-patch+json",
                "application/json",
                "",
            ):
                md = _update_managed_fields(md, "v1", field_manager, "Update", fields=patch_paths)
            spec_or_data = (
                merged.get("data")
                if plural in {"configmaps", "secrets"}
                else (
                    _service_account_spec_payload(merged)
                    if plural == "serviceaccounts"
                    else merged.get("spec")
                )
            )
            name_eff = md.get("name") or name
            ns_eff = (
                None
                if plural in {"namespaces", "persistentvolumes"}
                else (md.get("namespace") or ns)
            )
            if not _valid_name(name_eff):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.name (DNS-1123 label)",
                )
                return
            if ns_eff is not None and not _valid_name(ns_eff):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid metadata.namespace (DNS-1123 label)",
                )
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
            if patch_debug and plural not in {"configmaps", "secrets"}:
                LOGGER.info(
                    "patched %s/%s spec.replicas=%s",
                    plural,
                    name_eff,
                    (spec_or_data or {}).get("replicas"),
                )
            self._ok(_to_obj(updated))
            return
        orig_path = self.path
        self.path = path
        try:
            if self._handle_custom_resource_patch(ctype, patch):
                return
            if self._patch_extended_resources(ctype, patch, field_manager, force_flag):
                return
        finally:
            self.path = orig_path
        self._not_found()

    def _patch_extended_resources(
        self, ctype: str, patch: Any, field_manager: str, force_flag: bool
    ) -> bool:
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
            ("storage.k8s.io", "v1", "storageclasses", "StorageClass"),
            ("storage.k8s.io", "v1", "volumeattachments", "VolumeAttachment"),
            ("storage.k8s.io", "v1", "csidrivers", "CSIDriver"),
            ("storage.k8s.io", "v1", "csinodes", "CSINode"),
            ("storage.k8s.io", "v1", "csistoragecapacities", "CSIStorageCapacity"),
            ("snapshot.storage.k8s.io", "v1", "volumesnapshots", "VolumeSnapshot"),
            (
                "snapshot.storage.k8s.io",
                "v1",
                "volumesnapshotclasses",
                "VolumeSnapshotClass",
            ),
            (
                "snapshot.storage.k8s.io",
                "v1",
                "volumesnapshotcontents",
                "VolumeSnapshotContent",
            ),
        ]
        transform_map = {
            ("apps", "v1", "deployments"): _to_deployment,
            ("apps", "v1", "statefulsets"): _to_statefulset,
            ("apps", "v1", "daemonsets"): _to_daemonset,
            ("batch", "v1", "jobs"): _to_job,
            ("batch", "v1", "cronjobs"): _to_cronjob,
            ("autoscaling", "v2", "horizontalpodautoscalers"): lambda o: _to_hpa(
                o, self.server.store
            ),  # type: ignore[attr-defined]
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
                    if ctype.startswith("application/apply-patch"):
                        merged = self._apply_patch_merge({}, patch, ctype)
                        if merged is None:
                            return True
                        md = merged.get("metadata") or {}
                        patch_paths = (
                            _extract_field_paths(patch) if isinstance(patch, dict) else set()
                        )
                        if not patch_paths:
                            patch_paths = {"*"}
                        md = _update_managed_fields(
                            md,
                            f"{group}/{version}",
                            field_manager,
                            "Apply",
                            fields=patch_paths,
                            force=force_flag,
                        )
                        md = _ensure_managed_fields_entry(
                            md, f"{group}/{version}", field_manager, "Apply", patch_paths
                        )
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
                        if not _valid_name(name_eff):
                            self._json_status(
                                HTTPStatus.UNPROCESSABLE_ENTITY,
                                reason="Invalid",
                                message="invalid metadata.name (DNS-1123 label)",
                            )
                            return True
                        created = self.server.store.upsert(
                            group,
                            version,
                            res,
                            None,
                            name_eff,
                            metadata=_normalize_metadata(md, name_eff, None, res),
                            spec=_spec_payload(res, merged),
                            status=merged.get("status") or {},
                        )  # type: ignore[attr-defined]
                        self._ok(
                            transform_map.get(
                                (group, version, res), _to_generic(group, version, kind, res)
                            )(created)
                        )  # type: ignore[arg-type]
                        return True
                    self._not_found()
                    return True
                base = transform_map.get(
                    (group, version, res), _to_generic(group, version, kind, res)
                )(obj)  # type: ignore[arg-type]
                merged = self._apply_patch_merge(base, patch, ctype)
                if merged is None:
                    return True
                md = merged.get("metadata") or {}
                patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
                patch_debug = os.getenv("AE_APISHIM_PATCH_DEBUG", "0") == "1"
                if patch_debug:
                    LOGGER.info(
                        "patch %s/%s ctype=%s manager=%s paths=%s",
                        res,
                        name,
                        ctype or "<empty>",
                        field_manager,
                        sorted(patch_paths),
                    )
                if ctype.startswith("application/apply-patch"):
                    if _managed_conflict(obj.metadata, field_manager, patch_paths, force_flag):
                        self._json_status(
                            HTTPStatus.CONFLICT,
                            reason="Conflict",
                            message="managedFields conflict on apply",
                        )
                        return True
                    if not patch_paths:
                        patch_paths = {"*"}
                    md = _merge_dict(dict(obj.metadata), md)
                    md = _update_managed_fields(
                        md,
                        f"{group}/{version}",
                        field_manager,
                        "Apply",
                        fields=patch_paths,
                        force=force_flag,
                    )
                    md = _ensure_managed_fields_entry(
                        md, f"{group}/{version}", field_manager, "Apply", patch_paths
                    )
                elif ctype in (
                    "application/merge-patch+json",
                    "application/strategic-merge-patch+json",
                    "application/json",
                    "",
                ):
                    md = _update_managed_fields(
                        md, f"{group}/{version}", field_manager, "Update", fields=patch_paths
                    )
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
                if patch_debug and res in {"deployments", "statefulsets", "daemonsets"}:
                    spec_out = _spec_payload(res, merged)
                    LOGGER.info(
                        "patched %s/%s spec.replicas=%s", res, name_eff, spec_out.get("replicas")
                    )
                self._ok(
                    transform_map.get(
                        (group, version, res), _to_generic(group, version, kind, res)
                    )(updated)
                )  # type: ignore[arg-type]
                return True
            plural, ns, name = _gv_ns_name(self.path, group, version, res)
            if plural != res or not name:
                continue
            obj = self.server.store.get(group, version, res, ns, name)  # type: ignore[attr-defined]
            if not obj:
                if ctype.startswith("application/apply-patch"):
                    merged = self._apply_patch_merge({}, patch, ctype)
                    if merged is None:
                        return True
                    md = merged.get("metadata") or {}
                    patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
                    if not patch_paths:
                        patch_paths = {"*"}
                    md = _update_managed_fields(
                        md,
                        f"{group}/{version}",
                        field_manager,
                        "Apply",
                        fields=patch_paths,
                        force=force_flag,
                    )
                    md = _ensure_managed_fields_entry(
                        md, f"{group}/{version}", field_manager, "Apply", patch_paths
                    )
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
                    if not _valid_name(name_eff):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.name (DNS-1123 label)",
                        )
                        return True
                    if ns_eff is not None and not _valid_name(ns_eff):
                        self._json_status(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            reason="Invalid",
                            message="invalid metadata.namespace (DNS-1123 label)",
                        )
                        return True
                    created = self.server.store.upsert(
                        group,
                        version,
                        res,
                        ns_eff,
                        name_eff,
                        metadata=_normalize_metadata(md, name_eff, ns_eff, res),
                        spec=_spec_payload(res, merged),
                        status=merged.get("status") or {},
                    )  # type: ignore[attr-defined]
                    self._ok(
                        transform_map.get(
                            (group, version, res), _to_generic(group, version, kind, res)
                        )(created)
                    )  # type: ignore[arg-type]
                    return True
                self._not_found()
                return True
            base = transform_map.get((group, version, res), _to_generic(group, version, kind, res))(
                obj
            )  # type: ignore[arg-type]
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return True
            md = merged.get("metadata") or {}
            patch_paths = _extract_field_paths(patch) if isinstance(patch, dict) else set()
            patch_debug = os.getenv("AE_APISHIM_PATCH_DEBUG", "0") == "1"
            if patch_debug:
                LOGGER.info(
                    "patch %s/%s ctype=%s manager=%s paths=%s",
                    res,
                    name,
                    ctype or "<empty>",
                    field_manager,
                    sorted(patch_paths),
                )
            if ctype.startswith("application/apply-patch"):
                if _managed_conflict(obj.metadata, field_manager, patch_paths, force_flag):
                    self._json_status(
                        HTTPStatus.CONFLICT,
                        reason="Conflict",
                        message="managedFields conflict on apply",
                    )
                    return True
                if not patch_paths:
                    patch_paths = {"*"}
                md = _merge_dict(dict(obj.metadata), md)
                md = _update_managed_fields(
                    md,
                    f"{group}/{version}",
                    field_manager,
                    "Apply",
                    fields=patch_paths,
                    force=force_flag,
                )
                md = _ensure_managed_fields_entry(
                    md, f"{group}/{version}", field_manager, "Apply", patch_paths
                )
            elif ctype in (
                "application/merge-patch+json",
                "application/strategic-merge-patch+json",
                "application/json",
                "",
            ):
                md = _update_managed_fields(
                    md, f"{group}/{version}", field_manager, "Update", fields=patch_paths
                )
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
            if patch_debug and res in {"deployments", "statefulsets", "daemonsets"}:
                spec_out = _spec_payload(res, merged)
                LOGGER.info(
                    "patched %s/%s spec.replicas=%s", res, name_eff, spec_out.get("replicas")
                )
            self._ok(
                transform_map.get((group, version, res), _to_generic(group, version, kind, res))(
                    updated
                )
            )  # type: ignore[arg-type]
            return True
        return False

    def _apply_patch_merge(
        self, base: dict[str, Any], patch: Any, ctype: str
    ) -> dict[str, Any] | None:
        if ctype == "application/json-patch+json":
            merged = _apply_json_patch(base, patch if isinstance(patch, list) else [])
            if merged is None:
                self._json_status(
                    HTTPStatus.BAD_REQUEST, reason="Invalid", message="invalid json patch"
                )
            return merged
        if isinstance(patch, list) and ctype in ("application/json", ""):
            merged = _apply_json_patch(base, patch)
            if merged is None:
                self._json_status(
                    HTTPStatus.BAD_REQUEST, reason="Invalid", message="invalid json patch"
                )
            return merged
        if ctype in (
            "application/merge-patch+json",
            "application/strategic-merge-patch+json",
            "application/apply-patch+yaml",
            "application/apply-patch+json",
        ):
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
        self._refresh_crd_registry_from_state()
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
                self._json_status(
                    HTTPStatus.BAD_REQUEST,
                    reason="BadRequest",
                    message="resource is cluster-scoped; omit namespace",
                )
                return True

        def transform(obj: dict[str, Any]) -> dict[str, Any]:
            return _render_custom_resource(obj, group, version, meta.get("kind", plural))

        if name is None:
            if query.get("watch", ["0"])[0] in ("1", "true", "True"):
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
        # Validate ae.dev Deployment CRD payload against native schema.
        if (doc.get("apiVersion") or "").lower() not in {"ae.dev/v1alpha1"}:
            return "unsupported apiVersion for Deployment (expected ae.dev/v1alpha1)"
        if (doc.get("kind") or "").lower() != "deployment":
            return "unsupported kind for ae.dev/v1alpha1 (expected Deployment)"
        try:
            from ae.controller.spec import AppManifest  # imported lazily to avoid startup cost
        except Exception as exc:  # pragma: no cover - defensive import guard
            return f"unable to load Deployment schema: {exc}"
        try:
            AppManifest.model_validate(doc)
        except Exception as exc:
            return f"Deployment validation failed: {exc}"
        return None

    def _apply_app_admission(self, doc: dict[str, Any]) -> bool:
        err = self._validate_app_custom_resource(doc)
        if not err:
            return True
        mode = self._app_admission_mode()
        if mode == "warn":
            self._add_warning(err)
            return True
        if mode == "off":
            return True
        self._json_status(HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message=err)
        return False

    def _handle_custom_resource_post(self, doc: dict[str, Any]) -> bool:
        self._refresh_crd_registry_from_state()
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
            self._json_status(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                reason="Invalid",
                message="invalid metadata.name (DNS-1123 label)",
            )
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid or missing namespace",
                )
                return True
        else:
            ns_in = None
        if group == "ae.dev" and plural == "apps":
            if not self._apply_app_admission(doc):
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
        self._emit_warnings()
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
        return True

    def _handle_custom_resource_put(self, doc: dict[str, Any]) -> bool:
        self._refresh_crd_registry_from_state()
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
            self._json_status(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                reason="Invalid",
                message="invalid metadata.name (DNS-1123 label)",
            )
            return True
        if namespaced:
            if not ns_in or not _valid_name(ns_in):
                self._json_status(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    reason="Invalid",
                    message="invalid or missing namespace",
                )
                return True
        else:
            ns_in = None
        if group == "ae.dev" and plural == "apps":
            if not self._apply_app_admission(doc):
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
        self._refresh_crd_registry_from_state()
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
            self._json_status(
                HTTPStatus.UNPROCESSABLE_ENTITY, reason="Invalid", message="missing namespace"
            )
            return True
        if not namespaced:
            ns_eff = None
        if group == "ae.dev" and plural == "apps":
            if not self._apply_app_admission(merged):
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
        self._refresh_crd_registry_from_state()
        parsed = _parse_custom_resource_path(self.path)
        if not parsed:
            return False
        group, version, namespace, plural, name = parsed
        meta = self._lookup_crd(group, version, plural)
        if not meta:
            return False
        namespaced = meta.get("namespaced", True)
        if namespaced and namespace is None:
            self._json_status(
                HTTPStatus.BAD_REQUEST,
                reason="BadRequest",
                message="namespaced resource delete requires namespace",
            )
            return True
        store_ns = namespace if namespaced else None
        ok = self.server.store.delete(group, version, plural, store_ns, name)  # type: ignore[attr-defined]
        if not ok:
            self._not_found()
            return True
        self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
        return True

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authz():
            return
        path = urlparse(self.path).path
        if self._reject_ha_workload_mutation("DELETE", path):
            return
        plural, ns, name = _ns_name(path)
        if (
            plural
            in {
                "namespaces",
                "configmaps",
                "secrets",
                "persistentvolumeclaims",
                "persistentvolumes",
                "serviceaccounts",
                "services",
            }
            and name
        ):
            if not self._rbac_allows("delete", plural):
                self._deny(403)
                return
            ok = self.server.store.delete(
                "",
                "v1",
                plural,
                None if plural in {"namespaces", "persistentvolumes"} else ns,
                name,
            )  # type: ignore[attr-defined]
            if not ok:
                self._not_found()
                return
            self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
            return
        if path.startswith("/apis/batch/v1"):
            b_plural, b_ns, b_name = _batch_ns_name(path)
            if b_plural in {"jobs", "cronjobs"}:
                if not self._rbac_allows("delete", b_plural):
                    self._deny(403)
                    return
                if b_name:
                    ok = self.server.store.delete("batch", "v1", b_plural, b_ns, b_name)  # type: ignore[attr-defined]
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = (
                        self.server.store.list_all("batch", "v1", b_plural)  # type: ignore[attr-defined]
                        if b_ns is None
                        else self.server.store.list("batch", "v1", b_plural, b_ns)  # type: ignore[attr-defined]
                    )
                    for obj in items:
                        self.server.store.delete(
                            "batch", "v1", b_plural, obj.namespace or None, obj.name
                        )  # type: ignore[attr-defined]
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/storage.k8s.io/v1"):
            resources = (
                ("storageclasses", False),
                ("volumeattachments", False),
                ("csidrivers", False),
                ("csinodes", False),
                ("csistoragecapacities", True),
            )
            for plural, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(path, "storage.k8s.io", "v1", plural)
                    if s_plural != plural:
                        continue
                    if not self._rbac_allows("delete", plural):
                        self._deny(403)
                        return
                    if s_name:
                        ok = self.server.store.delete(  # type: ignore[attr-defined]
                            "storage.k8s.io", "v1", plural, s_ns, s_name
                        )
                        if not ok:
                            self._not_found()
                            return
                    else:
                        items = (
                            self.server.store.list_all("storage.k8s.io", "v1", plural)  # type: ignore[attr-defined]
                            if s_ns is None
                            else self.server.store.list("storage.k8s.io", "v1", plural, s_ns)  # type: ignore[attr-defined]
                        )
                        for obj in items:
                            self.server.store.delete(  # type: ignore[attr-defined]
                                "storage.k8s.io", "v1", plural, obj.namespace or None, obj.name
                            )
                    self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                    return

                s_plural, s_name = _gv_cluster_name(path, "storage.k8s.io", "v1", plural)
                if s_plural != plural:
                    continue
                if not self._rbac_allows("delete", plural):
                    self._deny(403)
                    return
                if s_name:
                    ok = self.server.store.delete(  # type: ignore[attr-defined]
                        "storage.k8s.io", "v1", plural, None, s_name
                    )
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = self.server.store.list_all("storage.k8s.io", "v1", plural)  # type: ignore[attr-defined]
                    for obj in items:
                        self.server.store.delete(  # type: ignore[attr-defined]
                            "storage.k8s.io", "v1", plural, None, obj.name
                        )
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/snapshot.storage.k8s.io/v1"):
            resources = (
                ("volumesnapshots", True),
                ("volumesnapshotclasses", False),
                ("volumesnapshotcontents", False),
            )
            for plural, namespaced in resources:
                if namespaced:
                    s_plural, s_ns, s_name = _gv_ns_name(
                        path, "snapshot.storage.k8s.io", "v1", plural
                    )
                    if s_plural != plural:
                        continue
                    if not self._rbac_allows("delete", plural):
                        self._deny(403)
                        return
                    if s_name:
                        ok = self.server.store.delete(  # type: ignore[attr-defined]
                            "snapshot.storage.k8s.io", "v1", plural, s_ns, s_name
                        )
                        if not ok:
                            self._not_found()
                            return
                    else:
                        items = (
                            self.server.store.list_all("snapshot.storage.k8s.io", "v1", plural)
                            if s_ns is None
                            else self.server.store.list(
                                "snapshot.storage.k8s.io", "v1", plural, s_ns
                            )
                        )  # type: ignore[attr-defined]
                        for obj in items:
                            self.server.store.delete(  # type: ignore[attr-defined]
                                "snapshot.storage.k8s.io",
                                "v1",
                                plural,
                                obj.namespace or None,
                                obj.name,
                            )
                    self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                    return

                s_plural, s_name = _gv_cluster_name(path, "snapshot.storage.k8s.io", "v1", plural)
                if s_plural != plural:
                    continue
                if not self._rbac_allows("delete", plural):
                    self._deny(403)
                    return
                if s_name:
                    ok = self.server.store.delete(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io", "v1", plural, None, s_name
                    )
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = self.server.store.list_all(  # type: ignore[attr-defined]
                        "snapshot.storage.k8s.io", "v1", plural
                    )
                    for obj in items:
                        self.server.store.delete(  # type: ignore[attr-defined]
                            "snapshot.storage.k8s.io", "v1", plural, None, obj.name
                        )
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(path)
            if d_plural in {"deployments", "statefulsets", "daemonsets"}:
                if not self._rbac_allows("delete", d_plural):
                    self._deny(403)
                    return
                if d_name:
                    ok = self.server.store.delete("apps", "v1", d_plural, d_ns, d_name)  # type: ignore[attr-defined]
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = (
                        self.server.store.list_all("apps", "v1", d_plural)  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", d_plural, d_ns)  # type: ignore[attr-defined]
                    )
                    for obj in items:
                        self.server.store.delete(
                            "apps", "v1", d_plural, obj.namespace or None, obj.name
                        )  # type: ignore[attr-defined]
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(path)
            if n_plural == "ingresses":
                if not self._rbac_allows("delete", n_plural):
                    self._deny(403)
                    return
                if n_name:
                    ok = self.server.store.delete("networking.k8s.io", "v1", n_plural, n_ns, n_name)  # type: ignore[attr-defined]
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = (
                        self.server.store.list_all("networking.k8s.io", "v1", n_plural)  # type: ignore[attr-defined]
                        if n_ns is None
                        else self.server.store.list("networking.k8s.io", "v1", n_plural, n_ns)  # type: ignore[attr-defined]
                    )
                    for obj in items:
                        self.server.store.delete(
                            "networking.k8s.io", "v1", n_plural, obj.namespace or None, obj.name
                        )  # type: ignore[attr-defined]
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/autoscaling/v2"):
            a_plural, a_ns, a_name = _gv_ns_name(
                path, "autoscaling", "v2", "horizontalpodautoscalers"
            )
            if a_plural == "horizontalpodautoscalers":
                if a_name:
                    ok = self.server.store.delete("autoscaling", "v2", a_plural, a_ns, a_name)  # type: ignore[attr-defined]
                    if not ok:
                        self._not_found()
                        return
                else:
                    items = (
                        self.server.store.list_all("autoscaling", "v2", a_plural)  # type: ignore[attr-defined]
                        if a_ns is None
                        else self.server.store.list("autoscaling", "v2", a_plural, a_ns)  # type: ignore[attr-defined]
                    )
                    for obj in items:
                        self.server.store.delete(
                            "autoscaling", "v2", a_plural, obj.namespace or None, obj.name
                        )  # type: ignore[attr-defined]
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
            )
            if crd_plural == "customresourcedefinitions" and crd_name:
                ok = self.server.store.delete(
                    "apiextensions.k8s.io", "v1", "customresourcedefinitions", None, crd_name
                )  # type: ignore[attr-defined]
                if not ok:
                    self._not_found()
                    return
                self._unregister_crd(crd_name)
                self._json_status(HTTPStatus.OK, reason="Success", message="deleted")
                return
        if self._handle_custom_resource_delete():
            return
        self._not_found()


def _kind(plural: str) -> str:
    return {
        "namespaces": "Namespace",
        "configmaps": "ConfigMap",
        "secrets": "Secret",
        "persistentvolumeclaims": "PersistentVolumeClaim",
        "persistentvolumes": "PersistentVolume",
        "serviceaccounts": "ServiceAccount",
        "services": "Service",
    }[plural]


def _api_version(group: str, version: str) -> str:
    return f"{group}/{version}" if group else version


_SECRET_TYPE_ANN = "ae.apishim/secret-type"


def _set_secret_type(md: dict[str, Any], secret_type: Any) -> None:
    if secret_type is None:
        return
    st = str(secret_type)
    if not st:
        return
    anns = md.get("annotations")
    if not isinstance(anns, dict):
        anns = {}
        md["annotations"] = anns
    anns[_SECRET_TYPE_ANN] = st


def _secret_type_from_meta(md: dict[str, Any]) -> str | None:
    anns = md.get("annotations")
    if not isinstance(anns, dict):
        return None
    val = anns.get(_SECRET_TYPE_ANN)
    if val is None:
        return None
    st = str(val)
    return st or None


def _service_account_spec_payload(doc: dict[str, Any]) -> dict[str, Any]:
    spec = doc.get("spec")
    out = dict(spec) if isinstance(spec, dict) else {}
    for key, value in doc.items():
        if key in {"apiVersion", "kind", "metadata", "status", "spec"}:
            continue
        out[key] = value
    return out


def _to_obj(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    secret_type = _secret_type_from_meta(meta) if o.resource == "secrets" else None
    return {
        "apiVersion": _api_version(o.group, o.version),
        "kind": _kind(o.resource),
        "metadata": meta,
        **({"type": secret_type} if secret_type else {}),
        **(
            {"data": o.spec}
            if o.resource in {"configmaps", "secrets"}
            else (
                ({} if not o.spec else dict(o.spec))
                if o.resource == "serviceaccounts"
                else ({} if not o.spec else {"spec": o.spec})
            )
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
    spec = dict(o.spec)
    if spec.get("replicas") is None:
        spec["replicas"] = int((o.status or {}).get("replicas", 1) or 1)
    status = _synthesize_deploy_status(spec, o.status)
    status["observedGeneration"] = meta["generation"]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta,
        "spec": spec,
        "status": status,
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
    status["observedGeneration"] = meta["generation"]
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
    st["observedGeneration"] = meta["generation"]
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


def _replicaset_name(dep_name: str) -> str:
    suffix = "-rs"
    max_len = 253
    if len(dep_name) + len(suffix) <= max_len:
        return f"{dep_name}{suffix}"
    return f"{dep_name[: max_len - len(suffix)]}{suffix}"


def _replicaset_labels(spec: dict[str, Any]) -> dict[str, str]:
    selector = spec.get("selector") or {}
    if isinstance(selector, dict):
        match_labels = selector.get("matchLabels")
        if isinstance(match_labels, dict):
            return {str(k): str(v) for k, v in match_labels.items()}
        return {str(k): str(v) for k, v in selector.items() if not isinstance(v, dict)}
    return {}


def _replicaset_from_deployment(dep: K8sObject) -> K8sObject:
    name = _replicaset_name(dep.name)
    meta = dict(dep.metadata or {})
    meta["name"] = name
    if dep.namespace:
        meta["namespace"] = dep.namespace
    meta.setdefault("uid", _stable_uid(dep.namespace, name, "replicasets"))
    labels = dict(meta.get("labels") or {})
    labels.update(_replicaset_labels(dep.spec or {}))
    meta["labels"] = labels
    annotations = dict(meta.get("annotations") or {})
    annotations.setdefault("deployment.kubernetes.io/revision", "1")
    meta["annotations"] = annotations
    owner_uid = (dep.metadata or {}).get("uid")
    if owner_uid:
        meta["ownerReferences"] = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": dep.name,
                "uid": owner_uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ]
    rs_spec = {
        "replicas": int((dep.spec or {}).get("replicas", 1) or 1),
        "selector": (dep.spec or {}).get("selector") or {},
        "template": (dep.spec or {}).get("template") or {},
    }
    gen = meta.get("generation")
    try:
        gen = int(gen) if gen is not None else 1
    except Exception:
        gen = 1
    rs_status = {
        "replicas": rs_spec["replicas"],
        "readyReplicas": rs_spec["replicas"],
        "availableReplicas": rs_spec["replicas"],
        "observedGeneration": gen,
    }
    return K8sObject(
        "apps",
        "v1",
        "replicasets",
        dep.namespace,
        name,
        meta,
        rs_spec,
        rs_status,
        dep.resource_version,
    )


def _to_replicaset(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata or {})
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    gen_val = meta.get("generation")
    try:
        gen = int(gen_val) if gen_val is not None else 1
    except Exception:
        gen = 1
    meta["generation"] = gen
    status = dict(o.status or {})
    status.setdefault("observedGeneration", gen)
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": meta,
        "spec": dict(o.spec),
        "status": status,
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
    spec_data = o.spec or {}
    spec = spec_data.get("spec", spec_data) if isinstance(spec_data, dict) else {}
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
        elif target_kind == "daemonset":
            obj = store.get("apps", "v1", "daemonsets", o.namespace, target_name)  # type: ignore[arg-type]
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


def _to_stored_event(o: K8sObject) -> dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    meta.setdefault("resourceVersion", str(o.resource_version))
    spec = dict(o.spec or {})
    out = {"apiVersion": "v1", "kind": "Event", "metadata": meta}
    for key in (
        "involvedObject",
        "reason",
        "message",
        "type",
        "source",
        "firstTimestamp",
        "lastTimestamp",
        "eventTime",
        "count",
        "action",
        "related",
        "reportingController",
        "reportingInstance",
    ):
        if key in spec:
            out[key] = spec[key]
    return out


def _ingress_vip(state: SQLiteStateStore, store: ObjectStore | None, ing: K8sObject) -> str | None:
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
        svc_obj = store.get("", "v1", "services", ing.namespace, svc_name) if store else None
    except Exception:
        svc_obj = None
    if svc_obj:
        prov_ip = _provider_cluster_ip(state, svc_obj, store)  # type: ignore[arg-type]
        return prov_ip or svc_obj.spec.get("clusterIP") or None
    return None


def _to_ingress(
    o: K8sObject, state: SQLiteStateStore | None = None, store: ObjectStore | None = None
) -> dict[str, Any]:
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
        vip = _ingress_vip(state, store, o)
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
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/replicasets(?:/([^/]+))?$", path)
    if m:
        return ("replicasets", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", None, m.group(1))
    m = re.match(r"^/apis/apps/v1/statefulsets(?:/([^/]+))?$", path)
    if m:
        return ("statefulsets", None, m.group(1))
    m = re.match(r"^/apis/apps/v1/daemonsets(?:/([^/]+))?$", path)
    if m:
        return ("daemonsets", None, m.group(1))
    m = re.match(r"^/apis/apps/v1/replicasets(?:/([^/]+))?$", path)
    if m:
        return ("replicasets", None, m.group(1))
    return ("", None, None)


def _net_ns_name(path: str) -> tuple[str, str | None, str | None]:
    m = re.match(r"^/apis/networking.k8s.io/v1/namespaces/([^/]+)/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", m.group(1), m.group(2))
    m = re.match(r"^/apis/networking.k8s.io/v1/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", None, m.group(1))
    return ("", None, None)


def _gv_ns_name(
    path: str, group: str, version: str, plural: str
) -> tuple[str, str | None, str | None]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/namespaces/([^/]+)/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1), m.group(2))
    pattern_all = (
        rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    )
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
        return merged.get("spec") or {}
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


def _stable_uid(ns: str | None, name: str, plural: str) -> str:
    seed = f"{plural}:{ns or ''}:{name}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()  # noqa: S324


def _normalize_metadata(
    md: dict[str, Any], name: str, ns: str | None, plural: str
) -> dict[str, Any]:
    out = dict(md)
    out["name"] = name
    if ns and plural != "namespaces":
        out["namespace"] = ns
    out.setdefault("uid", _stable_uid(ns, name, plural))
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


def _service_selector(spec: dict[str, Any]) -> dict[str, str]:
    raw = spec.get("selector") or {}
    selector: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if (
        "matchLabels" in selector
        and isinstance(selector.get("matchLabels"), dict)
        and len(selector) == 1
    ):
        selector = selector.get("matchLabels") or {}
    if not selector:
        maybe = (
            (spec.get("selector") or {}).get("matchLabels")
            if isinstance(spec.get("selector"), dict)
            else None
        )
        if isinstance(maybe, dict):
            selector = maybe
    if not isinstance(selector, dict):
        return {}
    return {str(k): str(v) for k, v in selector.items()}


def _pod_template_labels(obj: K8sObject) -> dict[str, str]:
    spec = obj.spec or {}
    template = spec.get("template") or {}
    meta = template.get("metadata") or {}
    labels = meta.get("labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items()}


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(key) == val for key, val in selector.items())


def _resolve_service_target(
    store: ObjectStore, svc: K8sObject, selector: dict[str, str]
) -> str | None:
    if not selector:
        return None
    ns = svc.namespace
    candidates: list[tuple[bool, int, int, str]] = []
    workloads = [
        ("apps", "v1", "deployments"),
        ("apps", "v1", "statefulsets"),
        ("apps", "v1", "daemonsets"),
    ]
    for order, (group, version, resource) in enumerate(workloads):
        try:
            items = (
                store.list(group, version, resource, ns)
                if ns is not None
                else store.list_all(group, version, resource)
            )
        except Exception:
            items = []
        for obj in items:
            labels = _pod_template_labels(obj)
            if not labels or not _selector_matches(selector, labels):
                continue
            exact = labels == selector
            extra = max(len(labels) - len(selector), 0)
            candidates.append((exact, extra, order, obj.name))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (not entry[0], entry[1], entry[2], entry[3]))
    return candidates[0][3]


def _service_target(svc: K8sObject, store: ObjectStore | None = None) -> str | None:
    spec = svc.spec or {}
    selector = _service_selector(spec)
    target = _resolve_service_target(store, svc, selector) if store else None
    if target:
        return target
    meta = svc.metadata or {}
    labels = meta.get("labels") if isinstance(meta, dict) else {}
    annotations = meta.get("annotations") if isinstance(meta, dict) else {}
    return (
        selector.get("app")
        or selector.get("app.kubernetes.io/name")
        or (labels.get("app") if isinstance(labels, dict) else None)
        or (annotations.get("apishim.k1s.dev/app") if isinstance(annotations, dict) else None)
        or svc.name
    )


def _service_app_name(svc: K8sObject, store: ObjectStore | None = None) -> str | None:
    tgt = _service_target(svc, store)
    if not tgt:
        return None
    return f"{svc.namespace}--{tgt}" if svc.namespace else tgt


def _provider_cluster_ip(
    state: SQLiteStateStore, svc: K8sObject, store: ObjectStore | None = None
) -> str | None:
    """Fetch cluster IP allocated by the network provider (if recorded in controller state)."""
    app_name = _service_app_name(svc, store)
    if not app_name:
        return None
    try:
        rec = state.get_service(app_name)  # type: ignore[attr-defined]
        return rec.cluster_ip if rec else None
    except Exception:
        return None


def _provider_ports(
    state: SQLiteStateStore, svc: K8sObject, store: ObjectStore | None = None
) -> dict:
    """Fetch provider-recorded port info (including nodePort) for a service, keyed by port name/number."""
    app_name = _service_app_name(svc, store)
    if not app_name:
        return {}
    try:
        rec = state.get_service(app_name)  # type: ignore[attr-defined]
    except Exception:
        rec = None
    if not rec or not rec.ports:
        return {}
    return rec.ports


def _provider_vip(
    state: SQLiteStateStore, svc: K8sObject, store: ObjectStore | None = None
) -> str | None:
    """Return overlay/proxy VIP if recorded by the network provider."""
    app_name = _service_app_name(svc, store)
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


def _merge_provider_service(
    state: SQLiteStateStore, store: ObjectStore | None, doc: dict[str, Any], svc_obj: K8sObject
) -> dict[str, Any]:
    """Augment service spec/status with provider allocations (clusterIP/nodePort)."""
    spec = doc.get("spec") or {}
    status = doc.get("status") or {}
    prov_ip = _provider_cluster_ip(state, svc_obj, store)
    vip = _provider_vip(state, svc_obj, store) or prov_ip
    if prov_ip:
        svc_type = (spec.get("type") or "ClusterIP") or "ClusterIP"
        # Prefer provider allocation unless service is explicitly headless/ExternalName.
        if svc_type != "ExternalName" and spec.get("clusterIP") != "None":
            spec["clusterIP"] = prov_ip
            # keep clusterIPs aligned when present
            if isinstance(spec.get("clusterIPs"), list):
                spec["clusterIPs"] = [prov_ip]
        # If loadBalancer ingress is present but disagrees with provider IP, reset it.
        if svc_type in {"LoadBalancer", "NodePort"}:
            lb = status.get("loadBalancer") if isinstance(status, dict) else None
            ingress = lb.get("ingress") if isinstance(lb, dict) else None
            if ingress:
                ingress_ips = {
                    str(i.get("ip")) for i in ingress if isinstance(i, dict) and i.get("ip")
                }
                if vip and str(vip) not in ingress_ips:
                    status = dict(status or {})
                    status["loadBalancer"] = {}
        status = _service_lb_status(spec, status, vip)
    # fill nodePorts from provider record if missing
    prov_ports = _provider_ports(state, svc_obj, store)
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
                zone = (rec.labels or {}).get("topology.kubernetes.io/zone") or (
                    rec.labels or {}
                ).get("failure-domain.beta.kubernetes.io/zone")
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


def _service_lb_status(
    spec: dict[str, Any], status: dict[str, Any], provider_ip: str | None = None
) -> dict[str, Any]:
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


def _endpoints_for_service(
    state: SQLiteStateStore, store: ObjectStore | None, svc: K8sObject
) -> dict[str, Any] | None:
    app_name = _service_app_name(svc, store)
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


def _endpointslice_for_service(
    state: SQLiteStateStore, store: ObjectStore | None, svc: K8sObject
) -> dict[str, Any] | None:
    """Project a single EndpointSlice per Service using controller endpoints."""
    target = _service_target(svc, store)
    if not target:
        return None
    from ae.controller.spec import app_key

    app_name = app_key(target, svc.namespace)
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


def _runtime_from_env_base(backend: str) -> RuntimeAdapter:
    if backend in {"stub", "test"}:
        return StubRuntime()
    if backend in {"cri", "containerd"}:
        return CRIRuntime()
    if backend in {"podman", "oci"}:
        try:
            return PodmanRuntime()
        except Exception:
            return DockerRuntime()
    return DockerRuntime()


def _runtime_from_env() -> RuntimeAdapter:
    backend = (os.getenv("AE_APISHIM_RUNTIME") or os.getenv("AE_RUNTIME_BACKEND") or "stub").lower()
    agent_url = os.getenv("AE_APISHIM_AGENT_URL") or os.getenv("AE_AGENT_URL")
    if backend == "remote" or agent_url:
        base_backend = (os.getenv("AE_RUNTIME_BACKEND") or "podman").lower()
        base = _runtime_from_env_base(base_backend)
        return RemoteRuntime(agent_url, base)  # type: ignore[arg-type]
    return _runtime_from_env_base(backend)


def _pod_obj(container: dict, rv: int, node_name: str | None) -> dict[str, Any]:
    labels = container.get("labels", {}) or {}
    pod_name = (
        labels.get("ae.pod_name") or labels.get("ae.replica_id") or container.get("name") or "pod"
    )
    ns = labels.get("ae.namespace") or "default"
    meta = {
        "name": pod_name,
        "namespace": ns,
        "labels": labels,
        "resourceVersion": str(rv),
    }
    uid = container.get("uid") or container.get("id")
    if uid:
        meta["uid"] = str(uid)
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


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, server_address: tuple[str, int], token: str | None, allow_anonymous: bool = False
    ) -> None:
        super().__init__(server_address, ShimHandler)
        ha_mode = str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}
        dsn = os.getenv("AE_APISHIM_DSN")
        db_path = Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))
        self.state = state_store_from_env()
        ShimHandler.state = self.state  # type: ignore[assignment]
        legacy_store = ObjectStore(db_path=db_path, dsn=dsn)
        self._legacy_store = legacy_store
        if ha_mode:
            self.store = MultiplexApishimStore.from_state_and_legacy(self.state, legacy_store)
        else:
            self.store = legacy_store
        self._storage_controller = None
        if ha_mode:
            LOGGER.info(
                "HA mode keeps the apishim storage controller disabled; leader-owned core storage reconcile runs from the main controller"
            )
        else:
            try:
                from ae.storage.controller import StorageController

                self._storage_controller = StorageController(self.store)
                seeded = self._storage_controller.sync()
                self._storage_controller.start()
                if seeded:
                    LOGGER.info("seeded %s StorageClass objects from config", seeded)
            except Exception as exc:  # noqa: BLE001
                self._storage_controller = None
                LOGGER.warning("storage controller init failed: %s", exc)
        ShimHandler.rehydrate_sa_tokens(self.store)
        ShimHandler.admin_token = token or os.getenv("AE_APISHIM_TOKEN")
        ShimHandler.read_token = os.getenv("AE_APISHIM_READ_TOKEN")
        ShimHandler.exec_token = os.getenv("AE_APISHIM_EXEC_TOKEN")
        ShimHandler.portforward_token = os.getenv("AE_APISHIM_PORTFORWARD_TOKEN")
        ShimHandler.mint_token = os.getenv("AE_APISHIM_MINT_TOKEN")
        ShimHandler.session_secret = os.getenv("AE_APISHIM_SESSION_SECRET")
        ShimHandler.pod_state_check = os.getenv("AE_APISHIM_POD_STATE_CHECK", "0") == "1"
        ShimHandler.pod_watch_check = os.getenv("AE_APISHIM_POD_WATCH_CHECK", "0") == "1"
        try:
            ShimHandler.pod_watch_ttl = float(
                os.getenv("AE_APISHIM_POD_WATCH_TTL_SECONDS", "30") or 30
            )
        except Exception:
            ShimHandler.pod_watch_ttl = 30.0
        ShimHandler.pod_watch_cache = {}
        ShimHandler.allow_anonymous = allow_anonymous
        self.runtime = _runtime_from_env()
        self._runtime_base = getattr(self.runtime, "_local", self.runtime)
        self._runtime_cache: dict[str, RuntimeAdapter] = {}
        self._agent_url = os.getenv("AE_APISHIM_AGENT_URL") or os.getenv("AE_AGENT_URL")
        self._bootstrap_crds()
        # Start adapter worker to reconcile apps/v1 Deployments into k1s
        adapter_enabled = str(os.getenv("AE_APISHIM_ADAPTER", "1") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        sot_enabled = str(os.getenv("AE_APISHIM_SOT", "0") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if ha_mode:
            adapter_enabled = False
        if sot_enabled:
            adapter_enabled = False
        if adapter_enabled:
            try:
                self._adapter = build_adapter(
                    self.store, runtime=self.runtime, state_store=self.state
                )
                self._adapter.start()
            except Exception:
                self._adapter = None
        else:
            self._adapter = None

    def _bootstrap_crds(self) -> None:
        if str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            ShimHandler._refresh_crd_registry_from_state(force=True)
            return
        try:
            objs = self.store.list_all("apiextensions.k8s.io", "v1", "customresourcedefinitions")
        except Exception:
            objs = []
        for obj in objs:
            ShimHandler._register_crd(obj)


def _wrap_store_errors(method):
    def _inner(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except RegistryConflictError as exc:
            self._json_status(
                HTTPStatus.CONFLICT,
                reason="Conflict",
                message=str(exc),
            )
            return None
        except AuthorityMutationError as exc:
            self._json_status(
                HTTPStatus(exc.status_code),
                reason=exc.reason,
                message=exc.message,
            )
            return None

    return _inner


ShimHandler.do_POST = _wrap_store_errors(ShimHandler.do_POST)
ShimHandler.do_PUT = _wrap_store_errors(ShimHandler.do_PUT)
ShimHandler.do_PATCH = _wrap_store_errors(ShimHandler.do_PATCH)
ShimHandler.do_DELETE = _wrap_store_errors(ShimHandler.do_DELETE)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8445,
    token: str | None = None,
    tls: bool = False,
    allow_anonymous: bool = False,
) -> None:
    if os.getenv("AE_APISHIM_ENABLE") != "1":
        raise RuntimeError("apishim disabled: set AE_APISHIM_ENABLE=1 to start the shim server")
    allow_anonymous = allow_anonymous or os.getenv("AE_APISHIM_ALLOW_ANON", "0") == "1"
    tok = token or os.getenv("AE_APISHIM_TOKEN")
    if not tok and not allow_anonymous:
        raise RuntimeError(
            "AE_APISHIM_TOKEN must be set (or --token) to start the shim server (or set AE_APISHIM_ALLOW_ANON=1 for dev)"
        )
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
