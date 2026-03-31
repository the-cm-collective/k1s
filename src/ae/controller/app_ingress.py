"""Derived EdgeIngressRoute sync for AppManifest ingress declarations."""

from __future__ import annotations

import logging
import os

from ae.controller.spec import AppManifest, DEFAULT_NAMESPACE

_LOG = logging.getLogger(__name__)
_TRANSLATED_FROM = "AppManifest"
_TRANSLATED_ANNOTATION = "k1s.io/translated-from"


def sync_translated_app_ingress(store, *, enabled: bool | None = None) -> None:  # type: ignore[no-untyped-def]
    """Synchronize translated ingress routes from registered apps.

    The controller owns these routes as derived state. Explicit user-authored
    EdgeIngressRoute resources always win on name collisions.
    """

    if enabled is None:
        enabled = _truthy_env("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS")
    if not enabled:
        return

    desired: dict[tuple[str, str], dict] = {}
    for entry in store.list_registered_apps():
        manifest = entry.manifest
        route = build_translated_route(manifest)
        if route is None:
            continue
        metadata = route["metadata"]
        key = (str(metadata["namespace"]), str(metadata["name"]))
        desired[key] = route

    existing_routes = {
        (route.namespace, route.name): route for route in store.list_edge_ingress_routes()
    }

    for key, route in desired.items():
        existing = existing_routes.get(key)
        if existing is not None and not edge_ingress_is_translated(existing):
            _LOG.warning(
                "skipping translated app ingress for %s/%s: explicit EdgeIngressRoute already exists",
                key[0],
                key[1],
            )
            continue

        spec = route["spec"]
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        placement = exposure.get("placement") if isinstance(exposure.get("placement"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        site_id = str(placement.get("site") or "core").strip() or "core"
        if mode in {"core-local", "core"}:
            site_id = "core"
        store.upsert_edge_ingress_route(
            name=str(route["metadata"]["name"]),
            namespace=str(route["metadata"]["namespace"]),
            site_id=site_id,
            policy_name=None,
            policy_namespace=None,
            document=route,
        )

    for key, route in existing_routes.items():
        if key in desired:
            continue
        if not edge_ingress_is_translated(route):
            continue
        store.delete_edge_ingress_route(name=route.name, namespace=route.namespace)


def build_translated_route(manifest: AppManifest) -> dict | None:
    ingress = getattr(manifest.spec, "ingress", None)
    if ingress is None:
        return None

    namespace = getattr(manifest.metadata, "namespace", None) or DEFAULT_NAMESPACE
    app_name = manifest.metadata.name
    route_name = f"{app_name}-ingress"
    mode = _translate_ingress_mode(manifest)
    site_id = _translate_ingress_site(mode, manifest)
    exposure: dict = {"mode": mode}
    if mode not in {"core-local", "core"}:
        exposure["placement"] = {"site": site_id}
    tls_block = _translate_ingress_tls(ingress)
    if tls_block:
        exposure["tls"] = tls_block

    port = _translate_ingress_port(manifest)
    paths_raw = list(getattr(ingress, "paths", []) or [])
    if not paths_raw:
        paths_raw = [getattr(ingress, "path", "/") or "/"]
    paths = []
    for item in paths_raw:
        path = str(item or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        service_ref = {
            "name": app_name,
            "namespace": namespace,
        }
        if port is not None:
            service_ref["port"] = port
        paths.append(
            {
                "path": path,
                "serviceRef": service_ref,
            }
        )

    return {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": {
            "name": route_name,
            "namespace": namespace,
            "annotations": {_TRANSLATED_ANNOTATION: _TRANSLATED_FROM},
        },
        "spec": {
            "host": ingress.host,
            "paths": paths,
            "exposure": exposure,
        },
    }


def edge_ingress_is_translated(record) -> bool:  # type: ignore[no-untyped-def]
    doc = record.spec if isinstance(record.spec, dict) else {}
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    annotations = metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    return annotations.get(_TRANSLATED_ANNOTATION) == _TRANSLATED_FROM


def _truthy_env(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _translate_ingress_mode(manifest: AppManifest) -> str:
    explicit_translate_mode = _normalize_ingress_mode(os.getenv("AE_EDGE_INGRESS_TRANSLATE_MODE"))
    if explicit_translate_mode:
        return explicit_translate_mode

    if _manifest_prefers_core_local(manifest):
        return "core-local"

    env_mode = _normalize_ingress_mode(
        os.getenv("AE_EDGE_INGRESS_MODE") or os.getenv("EDGE_INGRESS_MODE")
    )
    if env_mode:
        return env_mode

    backend = (os.getenv("AE_TRANSPORT_BACKEND") or "http").lower()
    if backend == "http":
        return "core-local"
    return "core-proxy"


def _normalize_ingress_mode(raw: str | None) -> str | None:
    value = str(raw or "").strip().lower().replace("_", "-")
    if value in {"core", "core-local"}:
        return "core-local"
    if value == "core-proxy":
        return "core-proxy"
    if value in {"core-to-edge-public", "core-to-edge", "public"}:
        return "core-to-edge-public"
    if value in {"edge-local", "edge"}:
        return "edge-local"
    return None


def _manifest_prefers_core_local(manifest: AppManifest) -> bool:
    try:
        node_selector = manifest.spec.node_selector or {}
    except Exception:
        node_selector = {}
    role = str(node_selector.get("role") or "").strip().lower()
    site = str(node_selector.get("site") or "").strip().lower()
    return role == "hub" or site == "hub"


def _translate_ingress_site(mode: str, manifest: AppManifest) -> str:
    site = (os.getenv("AE_EDGE_INGRESS_APP_SITE") or os.getenv("AE_SITE_ID") or "").strip()
    if not site:
        try:
            site = str((manifest.spec.node_selector or {}).get("site") or "").strip()
        except Exception:
            site = ""
    if not site or site == "core":
        site = "sfo-edge-01"
    if mode in {"core-local", "core"}:
        return "core"
    return site


def _translate_ingress_tls(ingress) -> dict | None:  # type: ignore[no-untyped-def]
    if getattr(ingress, "tls", True) is False and not getattr(ingress, "tls_secret_name", None):
        return None
    tls_block = {"mode": "terminate-core", "terminateCore": {"redirectHttpToHttps": True}}
    secret = getattr(ingress, "tls_secret_name", None)
    if secret:
        tls_block["terminateCore"]["secretName"] = str(secret)
    return tls_block


def _translate_ingress_port(manifest: AppManifest) -> int | None:
    try:
        if manifest.spec.health and manifest.spec.health.readiness:
            readiness = manifest.spec.health.readiness
            if getattr(readiness, "http_get", None) is not None:
                return int(readiness.http_get.port)
            if getattr(readiness, "tcp_socket", None) is not None:
                return int(readiness.tcp_socket.port)
    except Exception:
        pass
    try:
        if manifest.spec.service and getattr(manifest.spec.service, "target_port", None):
            return int(manifest.spec.service.target_port)
    except Exception:
        pass
    try:
        if manifest.spec.ports:
            return int(manifest.spec.ports[0].container_port)
    except Exception:
        pass
    return None
