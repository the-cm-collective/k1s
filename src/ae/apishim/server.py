from __future__ import annotations

import json
import os
import re
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import time
import json as _jsonlib

from .store import ObjectStore, K8sObject
from .adapter import build_adapter


K8S_VERSION = {
    "major": "0",
    "minor": "1",
    "gitVersion": "v0.1.0-k1s-shim",
}

RESERVED_GROUPS = {
    "",
    "apps",
    "networking.k8s.io",
    "rbac.authorization.k8s.io",
    "policy",
    "autoscaling",
    "apiextensions.k8s.io",
}


def _json(d: Dict[str, Any]) -> bytes:
    return json.dumps(d, separators=(",", ":")).encode("utf-8")


def _read_json(body: bytes) -> Dict[str, Any]:
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return {}


def _ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
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


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "k1s-apishim"
    token_required: Optional[str] = os.getenv("AE_APISHIM_TOKEN")
    store: ObjectStore
    crd_registry: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    crd_index: Dict[str, List[Tuple[str, str, str]]] = {}
    crd_lock = threading.RLock()

    def _authz(self) -> bool:
        if not self.token_required:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hdr[7:] == self.token_required:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", "Bearer")
        self._json_status(
            HTTPStatus.UNAUTHORIZED,
            reason="Unauthorized",
            message="missing/invalid bearer token",
        )
        return False

    def _ok(self, payload: Dict[str, Any]) -> None:
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

    def _stream_watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: Optional[str],
        query: Dict[str, List[str]],
        transform,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            start = time.time()
            timeout = int(query.get("timeoutSeconds", ["0"])[0] or 0) or None
            heartbeat = int(query.get("heartbeatSeconds", ["0"])[0] or 0) or None
            allow_bm = query.get("allowWatchBookmarks", ["0"])[0] in ("1", "true", "True")
            for ev_type, obj in self.server.store.watch(group, version, resource, namespace, heartbeat_seconds=heartbeat, allow_bookmarks=allow_bm):  # type: ignore[attr-defined]
                body = {"type": ev_type, "object": transform(obj)}
                line = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
                self.wfile.write(line)
                self.wfile.flush()
                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                    break
        except BrokenPipeError:
            pass

    def _serve_dynamic_group_discovery(self, path: str) -> bool:
        m_group = re.match(r"^/apis/([^/]+)$", path)
        if m_group:
            group = m_group.group(1)
            versions = self._crd_versions_for_group(group)
            if not versions:
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
    def _crd_versions_for_group(cls, group: str) -> List[str]:
        with cls.crd_lock:
            versions = sorted({ver for g, ver, _ in cls.crd_registry.keys() if g == group})
        return versions

    @classmethod
    def _dynamic_group_names(cls) -> List[str]:
        with cls.crd_lock:
            names = sorted({g for (g, _, _) in cls.crd_registry.keys()})
        return names

    @classmethod
    def _crd_resources_for(cls, group: str, version: str) -> List[Dict[str, Any]]:
        with cls.crd_lock:
            entries = [
                (plural, meta)
                for (g, v, plural), meta in cls.crd_registry.items()
                if g == group and v == version
            ]
        resources: List[Dict[str, Any]] = []
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
            keys: List[Tuple[str, str, str]] = []
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
    def _lookup_crd(cls, group: str, version: str, plural: str) -> Optional[Dict[str, Any]]:
        with cls.crd_lock:
            return cls.crd_registry.get((group, version, plural))

    def do_GET(self) -> None:  # noqa: N802
        if not self._authz():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if path == "/healthz" or path == "/readyz":
            self._ok({"status": "ok"})
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
            # Minimal swagger stub for client-go consumers
            doc = {
                "swagger": "2.0",
                "info": {"title": "k1s apishim", "version": "0.1.0"},
                "paths": {},
                "definitions": {},
            }
            self._ok(doc)
            return
        if path == "/api/v1":
            self._ok(
                {
                    "kind": "APIResourceList",
                    "groupVersion": "v1",
                    "resources": [
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
                if plural == "namespaces":
                    items = self.server.store.list("", "v1", "namespaces", None)  # type: ignore[attr-defined]
                else:
                    if ns is None:
                        items = self.server.store.list_all("", "v1", plural)  # type: ignore[attr-defined]
                    else:
                        items = self.server.store.list("", "v1", plural, ns)  # type: ignore[attr-defined]
                self._ok({"kind": f"{_kind(plural)}List", "apiVersion": "v1", "items": [_to_obj(i) for i in items]})
                return
            else:
                # GET
                obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_obj(obj))
                return

        # apps/v1 deployments
        if path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(path)
            if d_plural == "deployments":
                if d_name is None:
                    if q.get("watch", ["0"]) [0] in ("1", "true", "True"):
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
                    items = (
                        self.server.store.list_all("apps", "v1", "deployments")  # type: ignore[attr-defined]
                        if d_ns is None
                        else self.server.store.list("apps", "v1", "deployments", d_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "DeploymentList",
                            "apiVersion": "apps/v1",
                            "items": [_to_deployment(i) for i in items],
                        }
                    )
                    return
                else:
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
                                line = json.dumps({"type": ev_type, "object": _to_ingress(obj)}, separators=(",", ":")).encode("utf-8") + b"\n"
                                self.wfile.write(line)
                                self.wfile.flush()
                                if timeout is not None and timeout > 0 and (time.time() - start) >= timeout:
                                    break
                        except BrokenPipeError:
                            pass
                        return
                    items = (
                        self.server.store.list_all("networking.k8s.io", "v1", "ingresses")  # type: ignore[attr-defined]
                        if n_ns is None
                        else self.server.store.list("networking.k8s.io", "v1", "ingresses", n_ns)  # type: ignore[attr-defined]
                    )
                    self._ok(
                        {
                            "kind": "IngressList",
                            "apiVersion": "networking.k8s.io/v1",
                            "items": [_to_ingress(i) for i in items],
                        }
                    )
                    return
                else:
                    obj = self.server.store.get("networking.k8s.io", "v1", "ingresses", n_ns, n_name)  # type: ignore[attr-defined]
                    if not obj:
                        self._not_found()
                        return
                    self._ok(_to_ingress(obj))
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
                        self._stream_watch("autoscaling", "v2", "horizontalpodautoscalers", h_ns, q, transform=_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers"))
                        return
                    items = (
                        self.server.store.list_all("autoscaling", "v2", "horizontalpodautoscalers")  # type: ignore[attr-defined]
                        if h_ns is None
                        else self.server.store.list("autoscaling", "v2", "horizontalpodautoscalers", h_ns)  # type: ignore[attr-defined]
                    )
                    self._ok({"kind": "HorizontalPodAutoscalerList", "apiVersion": "autoscaling/v2", "items": [_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(i) for i in items]})
                    return
                obj = self.server.store.get("autoscaling", "v2", "horizontalpodautoscalers", h_ns, h_name)  # type: ignore[attr-defined]
                if not obj:
                    self._not_found()
                    return
                self._ok(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(obj))
                return
        self._not_found()

    def do_POST(self) -> None:  # noqa: N802
        if not self._authz():
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        doc = _read_json(body)

        plural, ns, name = _ns_name(self.path)
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
            created = self.server.store.upsert(  # type: ignore[attr-defined]
                "",
                "v1",
                plural,
                ns_in,
                name_in,
                metadata=_normalize_metadata(md, name_in, ns_in, plural),
                spec=doc.get("data") if plural in {"configmaps", "secrets"} else (doc.get("spec") or {}),
                status=doc.get("status") or {},
            )
            self.send_response(HTTPStatus.CREATED)
            out = _json(_to_obj(created))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        # apps/v1 deployments
        if self.path.startswith("/apis/apps/v1"):
            d_plural, d_ns, d_name = _apps_ns_name(self.path)
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
                created = self.server.store.upsert(  # type: ignore[attr-defined]
                    "apps",
                    "v1",
                    "deployments",
                    ns_in,
                    name_in,
                    metadata=_normalize_metadata(md, name_in, ns_in, "deployments"),
                    spec=doc.get("spec") or {},
                    status=_synthesize_deploy_status(doc.get("spec") or {}, doc.get("status") or {}),
                )
                self.send_response(HTTPStatus.CREATED)
                out = _json(_to_deployment(created))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
        # networking.k8s.io/v1 ingresses
        if self.path.startswith("/apis/networking.k8s.io/v1"):
            n_plural, n_ns, n_name = _net_ns_name(self.path)
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

        # CRDs
        if self.path.startswith("/apis/apiextensions.k8s.io/v1"):
            crd_plural, crd_name = _gv_cluster_name(
                self.path, "apiextensions.k8s.io", "v1", "customresourcedefinitions"
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
        if self.path.startswith("/apis/autoscaling/v2"):
            h_plural, h_ns, h_name = _gv_ns_name(self.path, "autoscaling", "v2", "horizontalpodautoscalers")
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
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
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
                self._ok(_to_generic("autoscaling", "v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")(updated))
                return
        self._not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authz():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        patch = _read_json(body)
        plural, ns, name = _ns_name(self.path)
        if plural in {"namespaces", "configmaps", "secrets", "serviceaccounts", "services"} and name:
            obj = self.server.store.get("", "v1", plural, None if plural == "namespaces" else ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return
            base = _to_obj(obj)
            if ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
                merged = _merge_dict(base, patch)
            elif ctype in ("application/json", ""):
                merged = patch  # full doc replace
            else:
                self._json_status(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    reason="UnsupportedMediaType",
                    message="only merge/strategic-merge or json supported",
                )
                return
            md = merged.get("metadata") or {}
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
        if self._handle_custom_resource_patch(ctype, patch):
            return
        if self._patch_extended_resources(ctype, patch):
            return
        self._not_found()

    def _patch_extended_resources(self, ctype: str, patch: Dict[str, Any]) -> bool:
        specs = [
            ("rbac.authorization.k8s.io", "v1", "roles", "Role"),
            ("rbac.authorization.k8s.io", "v1", "rolebindings", "RoleBinding"),
            ("rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole"),
            ("rbac.authorization.k8s.io", "v1", "clusterrolebindings", "ClusterRoleBinding"),
            ("policy", "v1", "poddisruptionbudgets", "PodDisruptionBudget"),
            ("autoscaling", "v2", "horizontalpodautoscalers", "HorizontalPodAutoscaler"),
        ]
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
                base = _to_generic(group, version, kind, res)(obj)
                merged = self._apply_patch_merge(base, patch, ctype)
                if merged is None:
                    return True
                md = merged.get("metadata") or {}
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
                self._ok(_to_generic(group, version, kind, res)(updated))
                return True
            plural, ns, name = _gv_ns_name(self.path, group, version, res)
            if plural != res or not name:
                continue
            obj = self.server.store.get(group, version, res, ns, name)  # type: ignore[attr-defined]
            if not obj:
                self._not_found()
                return True
            base = _to_generic(group, version, kind, res)(obj)
            merged = self._apply_patch_merge(base, patch, ctype)
            if merged is None:
                return True
            md = merged.get("metadata") or {}
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
            self._ok(_to_generic(group, version, kind, res)(updated))
            return True
        return False

    def _apply_patch_merge(
        self, base: Dict[str, Any], patch: Dict[str, Any], ctype: str
    ) -> Dict[str, Any] | None:
        if ctype in ("application/merge-patch+json", "application/strategic-merge-patch+json"):
            return _merge_dict(base, patch)
        if ctype in ("application/json", ""):
            return patch
        self._json_status(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            reason="UnsupportedMediaType",
            message="only merge/strategic-merge or json supported",
        )
        return None

    def _handle_custom_resource_get(self, path: str, query: Dict[str, List[str]]) -> bool:
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
        transform = lambda obj: _render_custom_resource(obj, group, version, meta.get("kind", plural))
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

    def _handle_custom_resource_post(self, doc: Dict[str, Any]) -> bool:
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

    def _handle_custom_resource_put(self, doc: Dict[str, Any]) -> bool:
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

    def _handle_custom_resource_patch(self, ctype: str, patch: Dict[str, Any]) -> bool:
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


def _to_obj(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
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


def _to_deployment(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
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


def _synthesize_deploy_status(spec: Dict[str, Any], base_status: Dict[str, Any]) -> Dict[str, Any]:
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


def _to_scale(o: K8sObject) -> Dict[str, Any]:
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


def _to_ingress(o: K8sObject) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    out = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta,
        "spec": dict(o.spec),
    }
    if o.status:
        out["status"] = o.status
    return out


def _to_crd(o: K8sObject) -> Dict[str, Any]:
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


def _apps_ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/namespaces/([^/]+)/deployments/([^/]+)/(status|scale)$", path)
    if m:
        return (f"deployments/{m.group(3)}", m.group(1), m.group(2))
    m = re.match(r"^/apis/apps/v1/deployments(?:/([^/]+))?$", path)
    if m:
        return ("deployments", None, m.group(1))
    return ("", None, None)


def _net_ns_name(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    m = re.match(r"^/apis/networking.k8s.io/v1/namespaces/([^/]+)/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", m.group(1), m.group(2))
    m = re.match(r"^/apis/networking.k8s.io/v1/ingresses(?:/([^/]+))?$", path)
    if m:
        return ("ingresses", None, m.group(1))
    return ("", None, None)


def _gv_ns_name(path: str, group: str, version: str, plural: str) -> Tuple[str, Optional[str], Optional[str]]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/namespaces/([^/]+)/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1), m.group(2))
    pattern_all = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern_all, path)
    if m:
        return (plural, None, m.group(1))
    return ("", None, None)


def _gv_cluster_name(path: str, group: str, version: str, plural: str) -> Tuple[str, Optional[str]]:
    pattern = rf"^/apis/{re.escape(group)}/{re.escape(version)}/{re.escape(plural)}(?:/([^/]+))?$"
    m = re.match(pattern, path)
    if m:
        return (plural, m.group(1))
    return ("", None)


def _to_generic(group: str, version: str, kind: str, resource: str):
    def convert(o: K8sObject) -> Dict[str, Any]:
        meta = dict(o.metadata)
        meta.setdefault("name", o.name)
        if o.namespace:
            meta.setdefault("namespace", o.namespace)
        body: Dict[str, Any] = {
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


def _spec_payload(resource: str, merged: Dict[str, Any]) -> Dict[str, Any]:
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


def _render_custom_resource(o: K8sObject, group: str, version: str, kind: str) -> Dict[str, Any]:
    meta = dict(o.metadata)
    meta.setdefault("name", o.name)
    if o.namespace:
        meta.setdefault("namespace", o.namespace)
    body: Dict[str, Any] = {
        "apiVersion": f"{group}/{version}",
        "kind": kind,
        "metadata": meta,
    }
    if o.spec:
        body["spec"] = o.spec
    if o.status:
        body["status"] = o.status
    return body


def _parse_custom_resource_path(path: str) -> Optional[Tuple[str, str, Optional[str], str, Optional[str]]]:
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


def _normalize_metadata(md: Dict[str, Any], name: str, ns: Optional[str], plural: str) -> Dict[str, Any]:
    out = dict(md)
    out["name"] = name
    if ns and plural != "namespaces":
        out["namespace"] = ns
    return out


def _merge_dict(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)  # type: ignore[arg-type]
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


class ShimServer(HTTPServer):
    def __init__(self, server_address: Tuple[str, int], token: Optional[str]) -> None:
        super().__init__(server_address, ShimHandler)
        self.store = ObjectStore()
        ShimHandler.token_required = token
        self._bootstrap_crds()
        # Start adapter worker to reconcile apps/v1 Deployments into k1s
        try:
            self._adapter = build_adapter(self.store)
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


def run_server(host: str = "127.0.0.1", port: int = 8445, token: Optional[str] = None, tls: bool = False) -> None:
    if os.getenv("AE_APISHIM_ENABLE") != "1":
        raise RuntimeError("apishim disabled: set AE_APISHIM_ENABLE=1 to start the shim server")
    httpd = ShimServer((host, port), token)
    if tls:
        # Dev TLS: requires user-provided cert/key via env or skip.
        cert_file = os.getenv("AE_APISHIM_TLS_CERT")
        key_file = os.getenv("AE_APISHIM_TLS_KEY")
        if not (cert_file and key_file):
            raise RuntimeError("TLS requested but AE_APISHIM_TLS_CERT/KEY not set")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
