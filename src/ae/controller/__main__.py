# ruff: noqa: E501,I001,E401,S110,S112,SIM105,SIM102,SIM114,B009,B904,UP017,UP006
"""Controller daemon entry point.

Usage:

  python -m ae.controller --once --specs specs/
  python -m ae.controller --loop --interval 5 --specs specs/ --metrics-port 9108

Imports manifests from the specs directory into the registry (source of truth),
then reconciles all registered apps either once or on a fixed interval. Optionally
serves a tiny Prometheus text endpoint.
"""

from __future__ import annotations

import argparse
import os
import time
import signal
import socket
import re
from urllib.parse import urlparse
from collections.abc import Iterable
from pathlib import Path
from datetime import datetime, timezone
import json, hashlib
import yaml

from ae.controller.state import SQLiteStateStore, state_store_from_env
from ae.controller.reconciler import Reconciler
from ae.controller.spec import (
    AppManifest,
    ManifestError,
    app_key_for_manifest,
    load_manifest,
    DEFAULT_NAMESPACE,
)
from ae.observability.http_api import start_http_api, set_reconcile_metrics
from ae.controller.agent_api import start_agent_api
from ae.observability.logging import configure_logging
from ae.config.transport import TransportConfig, check_nats_connectivity
from ae.transport.nats_client import NatsClient, NatsClientError
from ae.transport.controller_ingress import NatsControllerIngress
from ae.transport.telemetry_ingress import TelemetryIngress
from ae.transport.outbox_publisher import OutboxPublisher, OutboxPublisherConfig
from ae.controller.work_watchdog import WorkWatchdog, WorkWatchdogConfig
from ae.ingress.edge_core_proxy import EdgeCoreProxyRenderer, build_core_proxy_config
from ae.transport.route_bundle_publisher import RouteBundlePublisher, RouteBundlePublisherConfig
from ae.transport.jetstream_monitor import JetStreamMonitor, JetStreamMonitorConfig
from ae.cli.__main__ import (
    runtime_factory,
    health_manager_factory,
    ingress_service_factory,
    secret_manager_factory,
    config_manager_factory,
    registry_auth_factory,
    format_report,
)


def service_controller_factory(store: SQLiteStateStore):
    """Optional Service VIP controller (disabled by default)."""
    if os.getenv("AE_ENABLE_SERVICE_PROXY", "0") != "1":
        return None
    try:
        from ae.network import (
            DockerBridgeProvider,
            IptablesProvider,
            OverlayProvider,
            ServiceController,
        )

        backend = os.getenv("AE_RUNTIME_BACKEND", "podman").lower()
        provider_name = os.getenv(
            "AE_SERVICE_PROVIDER", "iptables" if backend in {"cri", "containerd"} else "bridge"
        ).lower()
        if backend in {"cri", "containerd"} and provider_name not in {
            "iptables",
            "kubeproxy",
            "cri",
        }:
            try:
                import logging

                logging.getLogger(__name__).warning(
                    "Service provider %s unsupported on CRI; using iptables.", provider_name
                )
            except Exception:
                pass
            provider_name = "iptables"
        if provider_name in {"iptables", "kubeproxy", "cri"}:
            provider = IptablesProvider(
                store,
                service_cidr=os.getenv("AE_SERVICE_IP_POOL", "10.241.0.0/16"),
                iptables_bin=os.getenv("AE_IPTABLES_BIN", "iptables"),
            )
        elif provider_name in {"overlay"}:
            provider = OverlayProvider(
                store,
                network_name=os.getenv("AE_OVERLAY_NET", "ae-overlay"),
                service_cidr=os.getenv("AE_SERVICE_IP_POOL", "10.241.0.0/16"),
                proxy_image=os.getenv("AE_SERVICE_PROXY_IMAGE", "haproxy:2.9-alpine"),
                docker_bin=os.getenv("AE_DOCKER_BIN", "docker"),
                manage_network=os.getenv("AE_OVERLAY_MANAGE_NETWORK", "1") == "1",
            )
        else:
            provider = DockerBridgeProvider(
                store,
                network_name=os.getenv("AE_NETWORK_NAME", "ae-net"),
                network_subnet=os.getenv(
                    "AE_NETWORK_SUBNET", os.getenv("AE_DOCKER_NETWORK_SUBNET", "")
                )
                or None,
                service_cidr=os.getenv("AE_SERVICE_IP_POOL", "10.241.0.0/16"),
                proxy_image=os.getenv("AE_SERVICE_PROXY_IMAGE", "haproxy:2.9-alpine"),
                docker_bin=os.getenv("AE_DOCKER_BIN", "docker"),
            )
        return ServiceController(provider, store)
    except Exception:
        return None


def _local_node_id() -> str:
    return os.getenv("AE_NODE_ID", socket.gethostname())


def _register_local_node(store: SQLiteStateStore, runtime_backend: str) -> None:
    """Best-effort local node registration for single-controller setups."""
    try:
        if store.list_nodes():
            return
        node_id = _local_node_id()
        name = os.getenv("AE_NODE_NAME", node_id)
        store.upsert_node(
            node_id,
            name=name,
            labels={"role": "controller"},
            taints=[],
            backend=runtime_backend,
            endpoint=None,
            pod_cidr=None,
            wg_pubkey=None,
        )
        store.record_heartbeat(node_id, "Ready")
    except Exception:
        pass


def _truthy_env(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_duration_seconds(value: str | None, default: float) -> float:
    if not value:
        return default
    raw = str(value).strip().lower()
    try:
        if raw.endswith("ms"):
            return float(raw[:-2]) / 1000.0
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except Exception:
        return default


def _parse_site_ids() -> list[str]:
    raw = (os.getenv("AE_SITE_IDS") or os.getenv("AE_SITE_ID") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _bootstrap_jetstream(transport: TransportConfig) -> None:
    if transport.backend != "nats-js":
        return
    if not transport.nats_url:
        return
    site_ids = _parse_site_ids()
    import logging as _log

    logger = _log.getLogger(__name__)
    stream_name = os.getenv("AE_JS_STREAM_NAME", "K1S_WORK")
    work_subject = os.getenv("AE_JS_WORK_SUBJECT", "k1s.v1.work.site.>")
    storage = os.getenv("AE_JS_STORAGE", "file")
    ack_wait_s = _parse_duration_seconds(
        os.getenv("AE_GATEWAY_JS_ACK_WAIT"), default=30.0
    )
    max_ack_pending = int(os.getenv("AE_GATEWAY_JS_MAX_ACK_PENDING", "32") or 32)
    max_deliver = int(os.getenv("AE_GATEWAY_JS_MAX_DELIVER", "20") or 20)
    max_waiting = int(os.getenv("AE_GATEWAY_JS_MAX_WAITING", "512") or 512)
    client = None
    try:
        client = NatsClient(
            url=transport.nats_url,
            creds=transport.nats_creds,
            name="k1s-js-bootstrap",
        )
        client.connect()
        client.ensure_stream(
            name=stream_name,
            subjects=[work_subject],
            storage=storage,
            retention="workqueue",
        )
        if not site_ids:
            logger.info(
                "AE_SITE_IDS not set; JS stream ready, consumers will be created on site register"
            )
        else:
            for site_id in site_ids:
                client.ensure_consumer(
                    stream=stream_name,
                    durable=f"WORK_SITE_{site_id}",
                    filter_subject=f"k1s.v1.work.site.{site_id}",
                    ack_wait_s=ack_wait_s,
                    max_ack_pending=max_ack_pending,
                    max_deliver=max_deliver,
                    max_waiting=max_waiting,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("jetstream bootstrap failed: %s", exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ae.controller", description="k1s controller daemon")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Reconcile once and exit")
    group.add_argument(
        "--loop", action="store_true", help="Run continuously and reconcile on an interval"
    )
    p.add_argument(
        "--specs",
        default=os.getenv("AE_SPECS_DIR", "specs"),
        help="Specs directory to import into the registry (source of truth)",
    )
    # Lower default polling interval to improve readiness after apply
    p.add_argument(
        "--interval",
        type=int,
        default=2,
        help="Polling interval in seconds for --loop",
    )
    p.add_argument(
        "--metrics-port",
        type=int,
        default=0,
        help="If set, serve Prometheus metrics on this TCP port",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Watch specs directory for changes (uses watchdog if available)",
    )
    p.add_argument("--debounce-ms", type=int, default=200, help="Debounce time for watch events")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    p.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)"
    )
    return p


def _find_manifests(specs_dir: Path) -> list[Path]:
    paths: list[Path] = []
    if not specs_dir.exists():
        return paths
    for p in specs_dir.rglob("*.y*"):
        if p.is_file():
            paths.append(p)
    return sorted(paths)


def _load_all(paths: Iterable[Path]) -> dict[str, tuple[AppManifest, Path]]:
    """Load manifests, preferring one file per app name and returning path for mtime."""
    selected: dict[str, tuple[AppManifest, Path]] = {}
    for path in paths:
        try:
            m = load_manifest(path)
        except ManifestError:
            continue
        app_name = app_key_for_manifest(m)
        cur = selected.get(app_name)
        if cur is None:
            selected[app_name] = (m, path)
            continue
        # Prefer files whose stem exactly equals the app name
        prefer_new = path.stem == m.metadata.name and cur[1].stem != m.metadata.name
        if prefer_new:
            selected[app_name] = (m, path)
    return selected


def _import_specs(specs_dir: Path, store: SQLiteStateStore, source: str = "specs") -> None:
    try:
        manifest_map = _load_all(_find_manifests(specs_dir))
    except Exception:
        return
    translate_ingress = _truthy_env("AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS")
    for manifest, _path in manifest_map.values():
        try:
            labels = getattr(getattr(manifest, "metadata", None), "labels", None)
            store.register_app(manifest, source=source, labels=labels)
            if translate_ingress:
                _translate_app_ingress(manifest, store)
        except Exception:
            continue


def _translate_app_ingress(manifest: AppManifest, store: SQLiteStateStore) -> None:
    ingress = getattr(manifest.spec, "ingress", None)
    if ingress is None:
        return
    ns = getattr(manifest.metadata, "namespace", None) or DEFAULT_NAMESPACE
    app_name = manifest.metadata.name
    route_name = f"{app_name}-ingress"
    existing = store.get_edge_ingress_route(name=route_name, namespace=ns)
    if existing is not None and not _edge_ingress_is_translated(existing):
        return
    mode = _translate_ingress_mode()
    site_id = _translate_ingress_site(mode)
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
    for p in paths_raw:
        path = str(p or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        entry = {
            "path": path,
            "serviceRef": {
                "name": app_name,
                "namespace": ns,
                **({"port": port} if port is not None else {}),
            },
        }
        paths.append(entry)

    payload = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": {
            "name": route_name,
            "namespace": ns,
            "annotations": {"k1s.io/translated-from": "AppManifest"},
        },
        "spec": {
            "host": ingress.host,
            "paths": paths,
            "exposure": exposure,
        },
    }
    store.upsert_edge_ingress_route(
        name=route_name,
        namespace=ns,
        site_id=site_id if mode not in {"core-local", "core"} else "core",
        policy_name=None,
        policy_namespace=None,
        document=payload,
    )


def _edge_ingress_is_translated(record) -> bool:
    doc = record.spec if isinstance(record.spec, dict) else {}
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    ann = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
    return ann.get("k1s.io/translated-from") == "AppManifest"


def _translate_ingress_mode() -> str:
    raw = (
        os.getenv("AE_EDGE_INGRESS_TRANSLATE_MODE")
        or os.getenv("AE_EDGE_INGRESS_MODE")
        or os.getenv("EDGE_INGRESS_MODE")
        or ""
    ).strip()
    if raw:
        raw = raw.lower().replace("_", "-")
        if raw in {"core", "core-local"}:
            return "core-local"
        if raw in {"core-proxy", "core-proxy"}:
            return "core-proxy"
        if raw in {"core-to-edge-public", "core-to-edge", "public"}:
            return "core-to-edge-public"
        if raw in {"edge-local", "edge"}:
            return "edge-local"
    backend = (os.getenv("AE_TRANSPORT_BACKEND") or "http").lower()
    if backend == "http":
        return "core-local"
    return "core-proxy"


def _translate_ingress_site(mode: str) -> str:
    site = (os.getenv("AE_EDGE_INGRESS_APP_SITE") or os.getenv("AE_SITE_ID") or "").strip()
    if not site or site == "core":
        site = "sfo-edge-01"
    if mode in {"core-local", "core"}:
        return "core"
    return site


def _translate_ingress_tls(ingress) -> dict | None:
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
            r = manifest.spec.health.readiness
            if getattr(r, "http_get", None) is not None:
                return int(r.http_get.port)
            if getattr(r, "tcp_socket", None) is not None:
                return int(r.tcp_socket.port)
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


def _iter_yaml_docs(paths: Iterable[Path]) -> Iterable[dict]:
    for path in paths:
        try:
            text = path.read_text()
        except Exception:
            continue
        try:
            docs = yaml.safe_load_all(text)
        except yaml.YAMLError:
            continue
        for doc in docs:
            if isinstance(doc, dict):
                yield doc


def _normalize_metadata(doc: dict) -> tuple[dict, str, str] | None:
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or "").strip()
    if not name:
        return None
    namespace = str(meta.get("namespace") or DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE
    normalized = dict(meta)
    normalized["name"] = name
    normalized["namespace"] = namespace
    return normalized, name, namespace


def _import_edge_ingress_specs(
    specs_dir: Path, store: SQLiteStateStore, source: str = "specs"
) -> None:
    try:
        paths = _find_manifests(specs_dir)
    except Exception:
        return
    for doc in _iter_yaml_docs(paths):
        api = str(doc.get("apiVersion") or "").strip()
        kind = str(doc.get("kind") or "").strip()
        if api != "k1s.io/v1":
            continue
        if kind == "EdgeIngressRoute":
            _store_edge_ingress_route(doc, store)
        elif kind == "EdgeIngressPolicy":
            _store_edge_ingress_policy(doc, store)
        elif kind == "SiteIngressEndpoint":
            _store_site_ingress_endpoint(doc, store)


def _store_edge_ingress_route(doc: dict, store: SQLiteStateStore) -> None:
    meta_info = _normalize_metadata(doc)
    if not meta_info:
        return
    meta, name, namespace = meta_info
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
    placement = (
        exposure.get("placement") if isinstance(exposure.get("placement"), dict) else {}
    )
    site_id = str(placement.get("site") or "").strip()
    policy_ref = spec.get("policyRef") if isinstance(spec.get("policyRef"), dict) else {}
    policy_name = str(policy_ref.get("name") or "").strip() or None
    policy_namespace = None
    if policy_name:
        policy_namespace = (
            str(policy_ref.get("namespace") or namespace).strip() or namespace
        )
    payload = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressRoute",
        "metadata": meta,
        "spec": spec,
    }
    store.upsert_edge_ingress_route(
        name=name,
        namespace=namespace,
        site_id=site_id,
        policy_name=policy_name,
        policy_namespace=policy_namespace,
        document=payload,
    )


def _store_edge_ingress_policy(doc: dict, store: SQLiteStateStore) -> None:
    meta_info = _normalize_metadata(doc)
    if not meta_info:
        return
    meta, name, namespace = meta_info
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    status = {"valid": isinstance(spec, dict), "errors": []}
    if not isinstance(spec, dict):
        status["errors"] = ["spec_missing_or_invalid"]
    payload = {
        "apiVersion": "k1s.io/v1",
        "kind": "EdgeIngressPolicy",
        "metadata": meta,
        "spec": spec,
    }
    store.upsert_edge_ingress_policy(
        name=name,
        namespace=namespace,
        document=payload,
        status=status,
    )


def _store_site_ingress_endpoint(doc: dict, store: SQLiteStateStore) -> None:
    meta_info = _normalize_metadata(doc)
    if not meta_info:
        return
    meta, name, _namespace = meta_info
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    status = doc.get("status") if isinstance(doc.get("status"), dict) else {}
    mode = str(spec.get("mode") or status.get("mode") or "").strip() or None
    public = spec.get("public") if isinstance(spec.get("public"), dict) else {}
    if not public:
        public = status.get("public") if isinstance(status.get("public"), dict) else {}
    urls = public.get("urls") if isinstance(public.get("urls"), list) else []
    core_proxy = spec.get("coreProxy") if isinstance(spec.get("coreProxy"), dict) else {}
    if not core_proxy:
        core_proxy = status.get("coreProxy") if isinstance(status.get("coreProxy"), dict) else {}
    core_proxy_port = core_proxy.get("upstreamPort")
    try:
        core_proxy_port = int(core_proxy_port) if core_proxy_port is not None else None
    except Exception:
        core_proxy_port = None
    if not mode:
        mode = "core-to-edge-public" if urls else "core-proxy"
    store.upsert_site_ingress_endpoint(
        site_id=name,
        mode=mode,
        core_proxy_port=core_proxy_port,
        public_urls=urls,
    )


def _reconcile_edge_ingress(store: SQLiteStateStore, edge_renderer=None) -> None:
    try:
        port_min = int(os.getenv("AE_CORE_PROXY_PORT_MIN", "18080") or 18080)
    except Exception:
        port_min = 18080
    try:
        port_max = int(os.getenv("AE_CORE_PROXY_PORT_MAX", "18999") or 18999)
    except Exception:
        port_max = 18999

    endpoints = {ep.site_id: ep for ep in store.list_site_ingress_endpoints()}
    policy_cache: dict[tuple[str, str], dict] = {}
    forward_auth_urls = _collect_core_forward_auth_urls(store, policy_cache)
    primary_forward_auth_url = sorted(forward_auth_urls)[0] if forward_auth_urls else None
    multiple_forward_auth = len(forward_auth_urls) > 1

    for route in store.list_edge_ingress_routes():
        spec = _edge_ingress_route_spec(route)
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        placement = (
            exposure.get("placement") if isinstance(exposure.get("placement"), dict) else {}
        )
        site_id = str(placement.get("site") or route.site_id or "").strip()
        errors: list[str] = []
        policy_unsupported: list[str] = []

        host = str(spec.get("host") or "").strip()
        if not host:
            errors.append("missing_host")
        if mode not in {"core-proxy", "core-to-edge-public", "edge-local", "core-local", "core"}:
            errors.append("invalid_exposure_mode")
        if mode not in {"core-local", "core"} and not site_id:
            errors.append("missing_site")

        if mode == "core-proxy" and site_id:
            ep = endpoints.get(site_id)
            if ep is None or ep.core_proxy_port is None:
                try:
                    store.ensure_site_ingress_port(
                        site_id,
                        port_min=port_min,
                        port_max=port_max,
                        mode="core-proxy",
                    )
                    ep = store.get_site_ingress_endpoint(site_id)
                    if ep:
                        endpoints[site_id] = ep
                except Exception:
                    ep = None
            if ep is None or ep.core_proxy_port is None:
                errors.append("missing_core_proxy_port")

        if mode == "core-to-edge-public" and site_id:
            ep = endpoints.get(site_id)
            if ep is None or not ep.public_urls:
                errors.append("missing_public_endpoint")

        policy_ref = spec.get("policyRef") if isinstance(spec.get("policyRef"), dict) else {}
        policy_name = str(policy_ref.get("name") or "").strip()
        policy_namespace = str(policy_ref.get("namespace") or route.namespace or "").strip()
        policy = None
        if policy_name:
            policy = _lookup_edge_ingress_policy(
                store, policy_name, policy_namespace, policy_cache
            )
            if policy is None:
                errors.append("policy_ref_not_found")
        if isinstance(policy, dict):
            if mode == "edge-local":
                policy_unsupported = _edge_local_policy_unsupported(policy)
            elif mode in {"core-proxy", "core-to-edge-public"}:
                errors.extend(
                    _core_policy_errors(
                        policy, primary_forward_auth_url, multiple_forward_auth
                    )
                )

        status = {
            "valid": len(errors) == 0,
            "errors": errors,
            "policyUnsupported": policy_unsupported,
            "observedAt": datetime.now(timezone.utc).isoformat(),
        }
        store.update_edge_ingress_route_status(
            name=route.name,
            namespace=route.namespace,
            status=status,
        )

    if edge_renderer is not None:
        try:
            edge_renderer.render()
        except Exception:
            pass


def _edge_local_policy_unsupported(spec: dict) -> list[str]:
    unsupported: list[str] = []
    allowed_top = {"timeouts", "websockets", "headers", "waf", "auth"}
    for key in spec.keys():
        if key not in allowed_top:
            unsupported.append(str(key))

    auth = spec.get("auth") if isinstance(spec.get("auth"), dict) else {}
    if auth:
        mode = str(auth.get("mode") or "").strip().lower()
        if mode and mode not in {"none"}:
            unsupported.append("auth")

    waf = spec.get("waf") if isinstance(spec.get("waf"), dict) else {}
    if waf:
        mode = str(waf.get("mode") or "none").strip().lower()
        if mode not in {"none", "basic"}:
            unsupported.append("waf.mode")
        basic = waf.get("basic") if isinstance(waf.get("basic"), dict) else None
        if mode == "basic":
            if basic is None:
                unsupported.append("waf.basic")
            else:
                allowed_basic = {"maxBodyBytes", "ipAllowlist", "ipDenylist"}
                for key in basic.keys():
                    if key not in allowed_basic:
                        unsupported.append(f"waf.basic.{key}")
                if "rateLimit" in basic:
                    unsupported.append("waf.basic.rateLimit")
        elif "basic" in waf:
            unsupported.append("waf.basic")

    stickiness = spec.get("stickiness") if isinstance(spec.get("stickiness"), dict) else {}
    if stickiness and stickiness.get("mode") not in {None, "none", "None"}:
        unsupported.append("stickiness")

    # websockets, timeouts, headers are allowed as-is
    return sorted(set(unsupported))


def _edge_ingress_route_spec(record) -> dict:
    doc = record.spec if isinstance(record.spec, dict) else {}
    if isinstance(doc.get("spec"), dict):
        return doc.get("spec") or {}
    return doc if isinstance(doc, dict) else {}


def _edge_ingress_policy_spec(record) -> dict | None:
    if record is None:
        return None
    doc = record.spec if isinstance(record.spec, dict) else {}
    if isinstance(doc.get("spec"), dict):
        return doc.get("spec")
    return doc if isinstance(doc, dict) else None


def _lookup_edge_ingress_policy(
    store: SQLiteStateStore,
    policy_name: str,
    policy_namespace: str,
    cache: dict[tuple[str, str], dict],
) -> dict | None:
    key = (policy_name, policy_namespace)
    policy = cache.get(key)
    if policy is not None:
        return policy
    record = store.get_edge_ingress_policy(name=policy_name, namespace=policy_namespace)
    spec = _edge_ingress_policy_spec(record) if record else None
    if isinstance(spec, dict):
        cache[key] = spec
        return spec
    return None


def _collect_core_forward_auth_urls(
    store: SQLiteStateStore, cache: dict[tuple[str, str], dict]
) -> set[str]:
    urls: set[str] = set()
    for route in store.list_edge_ingress_routes():
        spec = _edge_ingress_route_spec(route)
        exposure = spec.get("exposure") if isinstance(spec.get("exposure"), dict) else {}
        mode = str(exposure.get("mode") or "").strip().lower()
        if mode not in {"core-proxy", "core-to-edge-public"}:
            continue
        policy_ref = spec.get("policyRef") if isinstance(spec.get("policyRef"), dict) else {}
        policy_name = str(policy_ref.get("name") or "").strip()
        policy_namespace = str(policy_ref.get("namespace") or route.namespace or "").strip()
        if not policy_name:
            continue
        policy = _lookup_edge_ingress_policy(store, policy_name, policy_namespace, cache)
        if not isinstance(policy, dict):
            continue
        if _policy_auth_mode(policy) != "forward-auth":
            continue
        raw_url = _policy_forward_auth_raw(policy)
        normalized = _normalize_forward_auth_url(raw_url)
        if normalized:
            urls.add(normalized)
    return urls


def _core_policy_errors(
    policy: dict, primary_forward_auth_url: str | None, multiple_forward_auth: bool
) -> list[str]:
    errors: list[str] = []
    mode = _policy_auth_mode(policy)
    if mode and mode not in {"none", "forward-auth"}:
        errors.append(f"unsupported_auth_mode:{mode}")
    if mode == "forward-auth":
        raw_url = _policy_forward_auth_raw(policy)
        if not raw_url:
            errors.append("forward_auth_missing_url")
            return errors
        normalized = _normalize_forward_auth_url(raw_url)
        if normalized is None:
            errors.append("forward_auth_invalid_url")
            return errors
        if multiple_forward_auth and primary_forward_auth_url:
            if normalized != primary_forward_auth_url:
                errors.append("forward_auth_url_mismatch")
    return errors


def _policy_auth_mode(policy: dict) -> str:
    auth = policy.get("auth") if isinstance(policy.get("auth"), dict) else {}
    return str(auth.get("mode") or "").strip().lower()


def _policy_forward_auth_raw(policy: dict) -> str:
    auth = policy.get("auth") if isinstance(policy.get("auth"), dict) else {}
    forward = auth.get("forwardAuth") if isinstance(auth.get("forwardAuth"), dict) else {}
    return str(forward.get("url") or "").strip()


def _normalize_forward_auth_url(raw_url: str) -> str | None:
    if not raw_url:
        return None
    candidate = raw_url
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https"}:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or ""
    return f"{scheme}://{host}:{port}{path}"


def _env_true(name: str, default: str = "0") -> bool:
    raw = str(os.getenv(name, default) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _apishim_sot_enabled() -> bool:
    if os.getenv("AE_APISHIM_SOT") is not None:
        return _env_true("AE_APISHIM_SOT")
    return False


def _apishim_mirror_enabled() -> bool:
    if _apishim_sot_enabled():
        return True
    if os.getenv("AE_APISHIM_MIRROR") is not None:
        return _env_true("AE_APISHIM_MIRROR")
    return _env_true("AE_LABS")


_APISHIM_MIRROR_MODE: str | None = None
_APISHIM_MIRROR_STATS: dict[str, object] = {}


def _set_apishim_mirror_mode(mode: str, detail: str) -> None:
    global _APISHIM_MIRROR_MODE
    if mode == _APISHIM_MIRROR_MODE:
        return
    _APISHIM_MIRROR_MODE = mode
    try:
        import logging

        logging.getLogger(__name__).info("apishim mirror using %s (%s)", mode, detail)
    except Exception:
        pass


def _log_apishim_mirror_stats(
    *,
    reachable: bool,
    seen: int,
    created: int,
    updated: int,
    stale: int,
    ignored: int,
) -> None:
    global _APISHIM_MIRROR_STATS
    stats = {
        "reachable": bool(reachable),
        "seen": int(seen),
        "created": int(created),
        "updated": int(updated),
        "stale": int(stale),
        "ignored": int(ignored),
    }
    if stats == _APISHIM_MIRROR_STATS:
        return
    _APISHIM_MIRROR_STATS = stats
    try:
        import logging

        logging.getLogger(__name__).info(
            "apishim mirror sync: reachable=%s seen=%s created=%s updated=%s stale=%s ignored=%s",
            stats["reachable"],
            stats["seen"],
            stats["created"],
            stats["updated"],
            stats["stale"],
            stats["ignored"],
        )
    except Exception:
        pass


def _apishim_api_base() -> str:
    base = os.getenv("AE_APISHIM_SERVER") or os.getenv("AE_LABS_HELM_SERVER") or ""
    return base.strip().rstrip("/")


def _apishim_api_headers() -> dict[str, str]:
    token = (
        os.getenv("AE_APISHIM_READ_TOKEN")
        or os.getenv("AE_APISHIM_TOKEN")
        or os.getenv("AE_LABS_HELM_TOKEN")
        or ""
    ).strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _apishim_api_verify() -> bool | str:
    override = (
        os.getenv("AE_APISHIM_CA_BUNDLE")
        or os.getenv("AE_APISHIM_CA")
        or os.getenv("AE_APISHIM_TLS_CA")
        or ""
    ).strip()
    if override:
        try:
            path = Path(override)
            if path.exists():
                return str(path)
        except Exception:
            pass
    for path in ("state/certs/combined-dev-ca.pem", "state/profiles/labs/apishim.crt"):
        try:
            if Path(path).exists():
                return path
        except Exception:
            continue
    return False


def _apishim_api_get_json(
    url: str,
    headers: dict[str, str],
    verify: bool | str,
    timeout_seconds: float = 3.0,
) -> tuple[dict | None, bool]:
    try:
        import requests as _req

        resp = _req.get(url, headers=headers, timeout=timeout_seconds, verify=verify)
        if resp.status_code >= 400:
            return None, False
        if not resp.content:
            return {}, True
        return resp.json(), True
    except Exception:
        pass
    try:
        import json as _json
        import ssl as _ssl
        import urllib.request as _urlreq

        ctx = None
        if verify is False:
            ctx = _ssl._create_unverified_context()  # noqa: S323
        elif isinstance(verify, str):
            ctx = _ssl.create_default_context(cafile=verify)
        req = _urlreq.Request(url, headers=headers)  # noqa: S310
        with _urlreq.urlopen(req, timeout=timeout_seconds, context=ctx) as resp:  # noqa: S310
            if getattr(resp, "status", 200) >= 400:
                return None, False
            payload = resp.read()
        if not payload:
            return {}, True
        return _json.loads(payload), True
    except Exception:
        return None, False


def _snapshot_apishim_api_manifests(
    store: SQLiteStateStore,
    base: str,
) -> tuple[dict[str, AppManifest], bool]:
    try:
        from ae.apishim.adapter import AdapterWorker as _AdapterWorker
        from ae.apishim.adapter import _manifest_from_deployment as _shim_manifest
        from ae.apishim.store import K8sObject as _K8sObject
        from ae.runtime import StubRuntime as _StubRuntime
    except Exception:
        return {}, False

    base = base.strip().rstrip("/")
    if not base:
        return {}, False
    headers = _apishim_api_headers()
    verify = _apishim_api_verify()
    reachable = False
    namespaces: list[str] = []

    def _fetch(url: str) -> dict | None:
        nonlocal reachable
        data, ok = _apishim_api_get_json(url, headers, verify)
        if ok:
            reachable = True
            if isinstance(data, dict):
                return data
        return None

    ns_data = _fetch(f"{base}/api/v1/namespaces")
    if ns_data:
        for item in ns_data.get("items") or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            name = meta.get("name")
            if name:
                namespaces.append(str(name))
    if not namespaces:
        fallback = (
            os.getenv("AE_LABS_HELM_NAMESPACE") or os.getenv("AE_APISHIM_NAMESPACE") or ""
        ).strip()
        namespaces = [fallback] if fallback else ["demo-helm", "default"]

    items_by_key: dict[tuple[str, str, str, str], list[_K8sObject]] = {}
    services: list[_K8sObject] = []
    ingresses: list[_K8sObject] = []
    workloads: list[_K8sObject] = []

    def _add_item(group: str, version: str, resource: str, item: dict, ns_hint: str) -> None:
        meta = item.get("metadata") or {}
        name = meta.get("name")
        if not name:
            return
        ns_val = meta.get("namespace") or ns_hint or ""
        try:
            rv = int(meta.get("resourceVersion") or 0)
        except Exception:
            rv = 0
        obj = _K8sObject(
            group,
            version,
            resource,
            ns_val,
            name,
            meta,
            item.get("spec") or {},
            item.get("status") or {},
            rv,
        )
        key = (group, version, resource, ns_val or "")
        items_by_key.setdefault(key, []).append(obj)
        if group == "" and resource == "services":
            services.append(obj)
        elif group == "networking.k8s.io" and resource == "ingresses":
            ingresses.append(obj)
        elif (group, resource) in {
            ("apps", "deployments"),
            ("apps", "statefulsets"),
            ("apps", "daemonsets"),
            ("batch", "jobs"),
        }:
            workloads.append(obj)

    for ns in namespaces:
        svc_data = _fetch(f"{base}/api/v1/namespaces/{ns}/services")
        if svc_data:
            for item in svc_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("", "v1", "services", item, ns)
        ing_data = _fetch(f"{base}/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses")
        if ing_data:
            for item in ing_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("networking.k8s.io", "v1", "ingresses", item, ns)
        dep_data = _fetch(f"{base}/apis/apps/v1/namespaces/{ns}/deployments")
        if dep_data:
            for item in dep_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("apps", "v1", "deployments", item, ns)
        sts_data = _fetch(f"{base}/apis/apps/v1/namespaces/{ns}/statefulsets")
        if sts_data:
            for item in sts_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("apps", "v1", "statefulsets", item, ns)
        ds_data = _fetch(f"{base}/apis/apps/v1/namespaces/{ns}/daemonsets")
        if ds_data:
            for item in ds_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("apps", "v1", "daemonsets", item, ns)
        job_data = _fetch(f"{base}/apis/batch/v1/namespaces/{ns}/jobs")
        if job_data:
            for item in job_data.get("items") or []:
                if isinstance(item, dict):
                    _add_item("batch", "v1", "jobs", item, ns)

    if not reachable:
        return {}, False

    class _StoreView:
        def __init__(self, items: dict[tuple[str, str, str, str], list[_K8sObject]]) -> None:
            self._items = items

        def list(self, group: str, version: str, resource: str, namespace: str | None):
            key = (group, version, resource, namespace or "")
            return list(self._items.get(key, []))

        def list_all(self, group: str, version: str, resource: str):
            out: list[_K8sObject] = []
            for (g, v, r, _ns), items in self._items.items():
                if g == group and v == version and r == resource:
                    out.extend(items)
            return out

    class _NullReconciler:
        _runtime = _StubRuntime()

    helper = _AdapterWorker(_StoreView(items_by_key), store, _NullReconciler())  # type: ignore[arg-type]
    manifests: dict[str, AppManifest] = {}
    for svc in services:
        try:
            result = helper._service_spec_for(svc)
            if not result:
                continue
            dep_key, svc_spec = result
            helper._service_specs[dep_key] = svc_spec
            helper._service_name_map[(svc.namespace, svc.name)] = dep_key
        except Exception:
            continue
    for ing in ingresses:
        try:
            result = helper._ingress_spec_for(ing)
            if not result:
                continue
            dep_key, ing_spec = result
            helper._ingress_specs[dep_key] = ing_spec
            helper._ingress_owner_map[(ing.namespace, ing.name)] = dep_key
        except Exception:
            continue
    for obj in workloads:
        try:
            dep_key = (obj.namespace, obj.name)
            svc_spec = helper._service_specs.get(dep_key)
            ing_spec = helper._ingress_specs.get(dep_key)
            manifest = _shim_manifest(obj, service_spec=svc_spec, ingress_spec=ing_spec)
            manifests[app_key_for_manifest(manifest)] = manifest
        except Exception:
            continue

    return manifests, True


def _snapshot_apishim_manifests(
    store: SQLiteStateStore,
) -> tuple[dict[str, AppManifest], bool]:
    try:
        from ae.apishim.store import ObjectStore as _ObjectStore
        from ae.apishim.adapter import AdapterWorker as _AdapterWorker
        from ae.apishim.adapter import _manifest_from_deployment as _shim_manifest
        from ae.runtime import StubRuntime as _StubRuntime
    except Exception:
        return {}, False

    base = _apishim_api_base()
    dsn = os.getenv("AE_APISHIM_DSN")
    db_env = os.getenv("AE_APISHIM_DB")
    db_path = Path(db_env or "state/apishim.db")
    explicit_db = bool(db_env) and db_env != "state/apishim.db"
    prefer_api = bool(base) and not dsn and not explicit_db
    if prefer_api:
        manifests, reachable = _snapshot_apishim_api_manifests(store, base)
        if reachable:
            _set_apishim_mirror_mode("api", base or "api")
            return manifests, True

    if not dsn and not db_path.exists():
        if base:
            manifests, reachable = _snapshot_apishim_api_manifests(store, base)
            if reachable:
                _set_apishim_mirror_mode("api", base or "api")
                return manifests, True
        return {}, False

    try:
        shim_store = _ObjectStore(dsn=dsn) if dsn else _ObjectStore(db_path=db_path)
    except Exception:
        if base:
            manifests, reachable = _snapshot_apishim_api_manifests(store, base)
            if reachable:
                _set_apishim_mirror_mode("api", base or "api")
                return manifests, True
        return {}, False

    class _NullReconciler:
        _runtime = _StubRuntime()

    helper = _AdapterWorker(shim_store, store, _NullReconciler())  # type: ignore[arg-type]
    manifests: dict[str, AppManifest] = {}

    reachable = False
    try:
        services = shim_store.list_all("", "v1", "services")
        reachable = True
    except Exception:
        services = []
    for svc in services:
        try:
            result = helper._service_spec_for(svc)
            if not result:
                continue
            dep_key, svc_spec = result
            helper._service_specs[dep_key] = svc_spec
            helper._service_name_map[(svc.namespace, svc.name)] = dep_key
        except Exception:
            continue

    try:
        ingresses = shim_store.list_all("networking.k8s.io", "v1", "ingresses")
        reachable = True
    except Exception:
        ingresses = []
    for ing in ingresses:
        try:
            result = helper._ingress_spec_for(ing)
            if not result:
                continue
            dep_key, ing_spec = result
            helper._ingress_specs[dep_key] = ing_spec
            helper._ingress_owner_map[(ing.namespace, ing.name)] = dep_key
        except Exception:
            continue

    workloads = [
        ("apps", "v1", "deployments"),
        ("apps", "v1", "statefulsets"),
        ("apps", "v1", "daemonsets"),
        ("batch", "v1", "jobs"),
    ]
    for grp, ver, res in workloads:
        try:
            items = shim_store.list_all(grp, ver, res)
            reachable = True
        except Exception:
            items = []
        for obj in items:
            try:
                dep_key = (obj.namespace, obj.name)
                svc_spec = helper._service_specs.get(dep_key)
                ing_spec = helper._ingress_specs.get(dep_key)
                manifest = _shim_manifest(obj, service_spec=svc_spec, ingress_spec=ing_spec)
                manifests[app_key_for_manifest(manifest)] = manifest
            except Exception:
                continue

    if reachable:
        _set_apishim_mirror_mode("db", dsn or str(db_path))
        return manifests, True
    if base:
        manifests, reachable = _snapshot_apishim_api_manifests(store, base)
        if reachable:
            _set_apishim_mirror_mode("api", base or "api")
            return manifests, True
    return manifests, False


def _purge_app_from_runtime(reconciler: Reconciler, store: SQLiteStateStore, app: str) -> None:
    runtime = reconciler._runtime  # type: ignore[attr-defined]
    runtime.remove_app(app)
    ingress = reconciler._ingress_service  # type: ignore[attr-defined]
    if ingress is not None:
        try:
            ingress.remove(app)
            ingress.reload()
        except Exception:
            pass
    try:
        store.delete_registered_app(app)
    except Exception:
        pass
    try:
        store.delete_app_state(app, purge_history=True)
    except Exception:
        pass


def _sync_apishim_registry(
    store: SQLiteStateStore,
    reconciler: Reconciler,
    manifests: dict[str, AppManifest] | None = None,
    reachable: bool | None = None,
) -> bool:
    if not _apishim_mirror_enabled():
        return False

    if manifests is None or reachable is None:
        manifests, reachable = _snapshot_apishim_manifests(store)
    if not reachable:
        _log_apishim_mirror_stats(
            reachable=False,
            seen=0,
            created=0,
            updated=0,
            stale=0,
            ignored=0,
        )
        return False

    shim_seen = set(manifests.keys())
    created = 0
    updated = 0
    ignored = 0
    for name, manifest in manifests.items():
        try:
            existing = store.get_registered_entry(name)
        except Exception:
            existing = None
        if existing is not None and existing.source != "apishim":
            ignored += 1
            continue
        try:
            spec_hash = _spec_hash(manifest)
            if existing is not None and existing.spec_hash == spec_hash:
                ignored += 1
                continue
        except Exception:
            spec_hash = None
        try:
            labels = getattr(manifest.metadata, "labels", None)
            store.register_app(manifest, source="apishim", labels=labels)
            if existing is None:
                created += 1
            else:
                updated += 1
        except Exception:
            continue

    try:
        entries = store.list_registered_apps()
    except Exception:
        entries = []
    stale = [
        entry.app_name
        for entry in entries
        if entry.source == "apishim" and entry.app_name not in shim_seen
    ]
    for app in stale:
        _purge_app_from_runtime(reconciler, store, app)
    _log_apishim_mirror_stats(
        reachable=True,
        seen=len(shim_seen),
        created=created,
        updated=updated,
        stale=len(stale),
        ignored=ignored,
    )
    return True


def _spec_hash(manifest: AppManifest) -> str:
    payload = json.dumps(
        manifest.model_dump(by_alias=True, exclude_none=True), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _merge_file_and_db_manifests(
    file_map: dict[str, tuple[AppManifest, Path]],
    store: SQLiteStateStore,
    allowed: set[str] | None = None,
) -> dict[str, tuple[AppManifest, Path | None]]:
    """Prefer latest DB revision when it differs from on-disk spec, preserving file edits.

    - If hashes match, keep file manifest.
    - If hashes differ, choose the newer of file mtime vs DB revision.created_at.
    - Include DB-only apps (e.g., labs sessions) subject to 'allowed' filter.
    """
    merged: dict[str, tuple[AppManifest, Path | None]] = dict(file_map)

    def _db_created_ts(rev) -> float:
        try:
            ca = getattr(rev, "created_at", None)
            if ca is None:
                return 0.0
            if isinstance(ca, datetime):
                return ca.timestamp()
            return datetime.fromisoformat(str(ca)).timestamp()
        except Exception:
            return 0.0

    for status in store.list_status():
        name = status.app_name
        if allowed and name not in allowed:
            continue
        try:
            revs = store.list_revisions(name, limit=1)
            if not revs:
                continue
            rev = revs[0]
            db_manifest = store.get_revision_manifest(name, rev.revision)
            db_hash = rev.spec_hash or _spec_hash(db_manifest)
        except Exception:
            continue

        entry = merged.get(name)
        if entry is None:
            merged[name] = (db_manifest, None)
            continue

        file_manifest, file_path = entry
        try:
            file_hash = _spec_hash(file_manifest)
        except Exception:
            file_hash = ""

        if file_hash == db_hash:
            continue

        file_mtime = 0.0
        try:
            if file_path and file_path.exists():
                file_mtime = file_path.stat().st_mtime
        except Exception:
            file_mtime = 0.0

        if _db_created_ts(rev) >= file_mtime:
            merged[name] = (db_manifest, None)

    return merged


def _make_reconciler() -> Reconciler:
    store = state_store_from_env()
    registry_auth = registry_auth_factory()
    base_runtime = runtime_factory(registry_auth=registry_auth)
    agent_url = os.getenv("AE_AGENT_URL")
    try:
        from ae.runtime import RemoteRuntime

        runtime = RemoteRuntime(agent_url, base_runtime)
    except Exception:
        runtime = base_runtime
    health = health_manager_factory()
    ingress = ingress_service_factory()
    secrets = secret_manager_factory()
    configs = config_manager_factory()
    svc_controller = service_controller_factory(store)
    if _truthy_env("AE_REGISTER_LOCAL_NODE"):
        _register_local_node(store, runtime.__class__.__name__.lower())
    return Reconciler(
        runtime,
        store,
        health_manager=health,
        ingress_service=ingress,
        secret_manager=secrets,
        config_manager=configs,
        service_controller=svc_controller,
    )


def _reconcile_all(reconciler: Reconciler, manifests: Iterable[AppManifest]) -> None:
    import time as _t
    from ae.observability.http_api import record_app_reconcile, _labs_is_blocked
    import logging as _log

    for m in manifests:
        app_name = app_key_for_manifest(m)
        if _labs_is_blocked(app_name):
            try:
                _log.getLogger(__name__).debug(
                    "labs reset block: skipping reconcile for %s", app_name
                )
            except Exception:
                pass
            continue
        t0 = _t.time()
        try:
            report = reconciler.reconcile(m)
            dt = _t.time() - t0
            record_app_reconcile(
                app_name,
                dt,
                created=report.created,
                updated=report.updated,
                removed=report.removed,
            )
            print(format_report(report))
        except Exception as exc:  # pragma: no cover - defensive path
            # Do not crash the controller on a single manifest failure during demo/bootstrap.
            # Log the error, emit an event if the store is reachable, and continue.
            _log.getLogger(__name__).error("reconcile failed for %s: %s", app_name, exc)
            try:
                store = getattr(reconciler, "_state_store", None)
                if store is not None:
                    # Attribute to the latest revision if available; otherwise 0.
                    revs = store.list_revisions(app_name, limit=1)
                    rev = int(revs[0].revision) if revs else 0
                    store.record_event(app_name, rev, "ApplyError", str(exc))
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:  # pragma: no cover (covered via unit test paths)
    args = build_parser().parse_args(argv)
    specs_dir = Path(args.specs)
    # logging setup
    if args.verbose:
        configure_logging("DEBUG")
    elif args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)

    # Hint: log active SOPS age key file for secret decryption troubleshooting
    try:
        import logging as _log

        _log.getLogger(__name__).info(
            "SOPS_AGE_KEY_FILE=%s",
            os.getenv("SOPS_AGE_KEY_FILE", "<unset>"),
        )
    except Exception:
        pass

    transport = None
    try:
        import logging as _log

        transport = TransportConfig.from_env()
        logger = _log.getLogger(__name__)
        if transport.backend != "http":
            logger.info(
                "AE_TRANSPORT_BACKEND=%s configured; NATS ingress/outbox enabled",
                transport.backend,
            )
            if transport.nats_url:
                ok, detail = check_nats_connectivity(transport.nats_url)
                if ok:
                    logger.info("nats connectivity ok (%s)", detail)
                else:
                    logger.warning("nats connectivity failed (%s)", detail)
                if transport.backend in {"nats-core", "nats-js"}:
                    try:
                        nats_client = NatsClient(
                            url=transport.nats_url,
                            creds=transport.nats_creds,
                            name="k1s-controller",
                        )
                        nats_client.connect()
                        logger.info("nats client connected")
                    except NatsClientError as exc:
                        logger.warning("nats client unavailable: %s", exc)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("nats client connect failed: %s", exc)
            else:
                logger.warning("AE_NATS_URL not set; skipping nats connectivity check")
        else:
            logger.info("transport backend=%s", transport.backend)
    except Exception:
        pass

    if transport:
        _bootstrap_jetstream(transport)

    # Build reconciler (runtime, ingress, secrets, store)
    reconciler = _make_reconciler()
    store = state_store_from_env()
    _nats_ingress = None
    _telemetry_ingress = None
    _outbox_publisher = None
    _js_monitor = None
    _work_watchdog = None
    _route_bundle = None
    _edge_renderer = None
    if transport and transport.backend in {"nats-core", "nats-js"} and transport.nats_url:
        try:
            edge_cfg = build_core_proxy_config()
            if edge_cfg is not None:
                _edge_renderer = EdgeCoreProxyRenderer(store, edge_cfg)
            _nats_ingress = NatsControllerIngress(
                store,
                url=transport.nats_url,
                creds=transport.nats_creds,
                js_provision=transport.backend == "nats-js",
                edge_renderer=_edge_renderer,
            )
            _nats_ingress.start()
        except Exception as exc:  # noqa: BLE001
            import logging as _log

            _log.getLogger(__name__).warning("failed to start nats ingress: %s", exc)
        try:
            _telemetry_ingress = TelemetryIngress(
                url=transport.nats_url,
                creds=transport.nats_creds,
            )
            _telemetry_ingress.start()
        except Exception as exc:  # noqa: BLE001
            import logging as _log

            _log.getLogger(__name__).warning("failed to start telemetry ingress: %s", exc)
        if transport.backend == "nats-js":
            try:
                interval_s = float(
                    os.getenv("AE_OUTBOX_PUBLISH_INTERVAL_S", "0.5") or 0.5
                )
                batch_size = int(os.getenv("AE_OUTBOX_PUBLISH_BATCH", "100") or 100)
                _outbox_publisher = OutboxPublisher(
                    store,
                    nats_url=transport.nats_url,
                    nats_creds=transport.nats_creds,
                    config=OutboxPublisherConfig(interval_s=interval_s, batch_size=batch_size),
                )
                _outbox_publisher.start()
            except Exception as exc:  # noqa: BLE001
                import logging as _log

                _log.getLogger(__name__).warning("failed to start outbox publisher: %s", exc)
            try:
                watchdog_enabled = str(
                    os.getenv("AE_WORK_WATCHDOG", "1") or "1"
                ).lower() in {"1", "true", "yes", "on"}
                if watchdog_enabled:
                    dispatched_max = _parse_duration_seconds(
                        os.getenv("AE_WORK_DISPATCHED_MAX"), default=300.0
                    )
                    running_max = _parse_duration_seconds(
                        os.getenv("AE_WORK_RUNNING_MAX"), default=1800.0
                    )
                    watchdog_interval = _parse_duration_seconds(
                        os.getenv("AE_WORK_WATCHDOG_INTERVAL"), default=5.0
                    )
                    _work_watchdog = WorkWatchdog(
                        store,
                        config=WorkWatchdogConfig(
                            interval_s=watchdog_interval,
                            dispatched_max_s=dispatched_max,
                            running_max_s=running_max,
                        ),
                    )
                    _work_watchdog.start()
            except Exception as exc:  # noqa: BLE001
                import logging as _log

                _log.getLogger(__name__).warning("failed to start work watchdog: %s", exc)
            try:
                monitor_interval = float(
                    os.getenv("AE_JS_MONITOR_INTERVAL_S", "10") or 10
                )
            except Exception:
                monitor_interval = 10.0
            if monitor_interval > 0:
                try:
                    stream_name = os.getenv("AE_JS_STREAM_NAME", "K1S_WORK")
                    site_ids = _parse_site_ids()
                    _js_monitor = JetStreamMonitor(
                        nats_url=transport.nats_url,
                        nats_creds=transport.nats_creds,
                        config=JetStreamMonitorConfig(
                            interval_s=monitor_interval,
                            stream_name=stream_name,
                            site_ids=site_ids,
                        ),
                    )
                    _js_monitor.start()
                except Exception as exc:  # noqa: BLE001
                    import logging as _log

                    _log.getLogger(__name__).warning("failed to start js monitor: %s", exc)
        if transport.backend in {"nats-core", "nats-js"}:
            try:
                bundle_enabled = str(
                    os.getenv("AE_ROUTE_BUNDLE_ENABLED", "0") or "0"
                ).lower() in {"1", "true", "yes", "on"}
                if bundle_enabled:
                    bundle_interval = float(
                        os.getenv("AE_ROUTE_BUNDLE_INTERVAL_S", "5") or 5
                    )
                    _route_bundle = RouteBundlePublisher(
                        store,
                        nats_url=transport.nats_url,
                        nats_creds=transport.nats_creds,
                        config=RouteBundlePublisherConfig(interval_s=bundle_interval),
                    )
                    _route_bundle.start()
            except Exception as exc:  # noqa: BLE001
                import logging as _log

                _log.getLogger(__name__).warning("failed to start route bundle: %s", exc)
    _agent_api_server = None
    try:
        agent_port = int(os.getenv("AE_AGENT_API_PORT", os.getenv("AE_AGENT_PORT", "0") or 0))
    except Exception:
        agent_port = 0
    if agent_port > 0:
        try:
            agent_host = os.getenv("AE_AGENT_API_HOST", "0.0.0.0")  # noqa: S104 - agent API must be reachable by nodes
            agent_token = os.getenv("AE_AGENT_API_TOKEN")
            _agent_api_server = start_agent_api(
                state_store_from_env(),
                host=agent_host,
                port=agent_port,
                token=agent_token,
                tls_cert=os.getenv("AE_AGENT_API_TLS_CERT"),
                tls_key=os.getenv("AE_AGENT_API_TLS_KEY"),
                client_ca=os.getenv("AE_AGENT_API_CLIENT_CA"),
                require_client_cert=os.getenv("AE_AGENT_API_REQUIRE_CLIENT_CERT", "0") == "1",
            )
        except Exception as exc:
            import logging as _log

            _log.getLogger(__name__).warning("failed to start agent API: %s", exc)

    # Initialize HTTP API server (metrics/status/events) and optional mutators if requested
    api_server = None
    if args.metrics_port and args.metrics_port > 0:
        # Optional mutators wired via closures and gated at handler level
        def _scale(app: str, replicas: int):  # noqa: ANN001
            revs = store.list_revisions(app, limit=1)
            if not revs:
                raise RuntimeError(f"no revisions recorded for {app}")
            manifest = store.get_revision_manifest(app, revs[0].revision)
            new_spec = manifest.spec.model_copy(update={"replicas": int(replicas)})
            updated = manifest.model_copy(update={"spec": new_spec})
            try:
                existing = store.get_registered_entry(app)
                src = existing.source if existing else "api"
                lbls = existing.labels if existing else None
                store.register_app(updated, source=src, labels=lbls)
            except Exception:
                pass

            # First reconcile immediately
            report = reconciler.reconcile(updated)

            # Optional fast-follow burst: perform a few short-interval reconciles
            # to shorten the time from progressing->ready for demo/playground flows.
            try:
                import os as _os, time as _t

                burst = int(_os.getenv("AE_SCALE_RECONCILE_BURST", "2"))
                delay_ms = int(_os.getenv("AE_SCALE_RECONCILE_DELAY_MS", "300"))
                burst = max(0, burst)
                for _ in range(burst):
                    if str(report.revision_status).lower() == "ready":
                        break
                    _t.sleep(max(0.001, delay_ms / 1000.0))
                    report = reconciler.reconcile(updated)
            except Exception:
                # Best-effort only; never fail the scale on fast-follow errors
                pass

            return {
                "app": app,
                "replicas": int(replicas),
                "revision": report.revision,
                "status": report.revision_status,
                "created": report.created,
                "updated": report.updated,
                "removed": report.removed,
            }

        def _delete(app: str, purge: bool):  # noqa: ANN001
            # Remove runtime containers
            runtime = reconciler._runtime  # type: ignore[attr-defined]
            removed = runtime.remove_app(app)
            # Remove ingress if present
            ingress = reconciler._ingress_service  # type: ignore[attr-defined]
            if ingress is not None:
                try:
                    ingress.remove(app)
                    ingress.reload()
                except Exception:
                    pass
            try:
                store.delete_registered_app(app)
            except Exception:
                pass
            store.delete_app_state(app, purge_history=bool(purge))
            return {"app": app, "removed": removed, "purged": bool(purge)}

        def _apply(payload: dict, source: str | None = None, labels: dict | None = None):  # noqa: ANN001
            # Accept a Deployment/App manifest JSON and reconcile
            from ae.controller.spec import AppManifest

            try:
                manifest = AppManifest.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"invalid manifest: {exc}")
            warnings: list[str] = []
            if str(getattr(manifest, "kind", "")) == "App":
                warnings.append(
                    "kind 'App' is deprecated; use kind 'Deployment' (apiVersion: ae.dev/v1alpha1)"
                )
            try:
                app_name = app_key_for_manifest(manifest)
                existing = store.get_registered_entry(app_name)
                src = source or (existing.source if existing else "api")
                lbls = labels if labels is not None else (existing.labels if existing else None)
                store.register_app(manifest, source=src, labels=lbls)
            except Exception:
                pass
            # First reconcile immediately
            report = reconciler.reconcile(manifest)

            # Optional fast-follow burst: perform a few short-interval reconciles
            # to shorten the time from progressing->ready for demo/playground flows.
            try:
                import os as _os, time as _t

                burst = int(_os.getenv("AE_APPLY_RECONCILE_BURST", "2"))
                delay_ms = int(_os.getenv("AE_APPLY_RECONCILE_DELAY_MS", "300"))
                burst = max(0, burst)
                for _ in range(burst):
                    if str(report.revision_status).lower() == "ready":
                        break
                    _t.sleep(max(0.001, delay_ms / 1000.0))
                    report = reconciler.reconcile(manifest)
            except Exception:
                # Best-effort only; never fail the apply on fast-follow errors
                pass

            return {
                "app": report.app_name,
                "revision": report.revision,
                "status": report.revision_status,
                "created": report.created,
                "updated": report.updated,
                "removed": report.removed,
                **({"warnings": warnings} if warnings else {}),
            }

        def _logs(
            app: str, container: str | None, tail: int | None, since: int | None, follow: bool
        ):
            def _select_target(reps):
                if not reps:
                    return None
                # Prefer pods from the current revision when known, and pick ready/live first.
                rev = None
                try:
                    st = store.get_status(app)
                    if st is not None and st.revision is not None:
                        rev = int(st.revision)
                except Exception:
                    rev = None

                candidates = reps
                if rev is not None:
                    rev_tag = f"-rev{rev}-"
                    by_rev = [r for r in reps if rev_tag in r.pod_name]
                    if by_rev:
                        candidates = by_rev

                def _pod_rev(name: str) -> int:
                    m = re.search(r"-rev(\d+)-", name)
                    return int(m.group(1)) if m else -1

                def _rank(pod) -> tuple[int, int, str]:
                    ready = 1 if (pod.ready or pod.live) else 0
                    return (ready, _pod_rev(pod.pod_name), pod.pod_name)

                return max(candidates, key=_rank)

            def _is_shutdown_line(line: str) -> bool:
                lowered = line.lower()
                return (
                    "got a kill signal" in lowered
                    or "servers closed" in lowered
                    or "asking process to exit" in lowered
                )

            reps = store.list_pods(app)
            target = None
            if container:
                # Prefer runtime's container-specific logs API when available
                rt = getattr(reconciler, "_runtime", None)
                if rt is not None and hasattr(rt, "read_logs_for_container"):
                    fn = getattr(rt, "read_logs_for_container")
                    return fn(app, str(container), follow=follow, tail=tail, since=since)
                # Fallback to pod-name matching
                sel = str(container)
                for r in reps:
                    if r.pod_name == sel or sel in r.pod_name:
                        target = r
                        break

            if not target and reps:
                target = _select_target(reps)

            if not follow:
                if not target:
                    return []
                return reconciler._runtime.read_logs(
                    target.pod_name, follow=False, tail=tail, since=since
                )

            # Follow mode: if the target exits (e.g., scaling/rollout), reselect and continue.
            if container:
                if not target:
                    return []
                return reconciler._runtime.read_logs(
                    target.pod_name, follow=True, tail=tail, since=since
                )

            def _stream_follow():
                last_since = since
                while True:
                    reps_local = store.list_pods(app)
                    target_local = _select_target(reps_local)
                    if not target_local:
                        time.sleep(0.5)
                        continue
                    try:
                        for line in reconciler._runtime.read_logs(
                            target_local.pod_name,
                            follow=True,
                            tail=tail,
                            since=last_since,
                        ):
                            text = (
                                line.decode("utf-8", "replace")
                                if isinstance(line, (bytes, bytearray))
                                else str(line)
                            )
                            if _is_shutdown_line(text):
                                break
                            yield text
                    except Exception:
                        pass
                    # After the first pass, drop "since" to avoid skipping new logs
                    last_since = None
                    # Short pause before reselecting (avoid tight loops on failures)
                    time.sleep(0.2)

            return _stream_follow()

        def _exec(app: str, container: str | None, cmd: list[str], timeout: int | None) -> int:
            # Prefer runtime container-scoped exec when container is provided
            if container:
                rt = getattr(reconciler, "_runtime", None)
                if rt is not None and hasattr(rt, "exec_for_container"):
                    return int(
                        getattr(rt, "exec_for_container")(app, str(container), cmd, timeout=timeout)
                    )
                # Fallback: run in a matching pod name
                reps = store.list_pods(app)
                target = next(
                    (
                        r
                        for r in reps
                        if (r.pod_name == container or str(container) in r.pod_name)
                    ),
                    None,
                )
                if target is None and reps:
                    target = reps[0]
                if target is None:
                    return 127
                return int(reconciler._runtime.exec(target.pod_name, cmd, timeout=timeout))
            # Default: pick a ready pod
            reps = store.list_pods(app)
            target = next((r for r in reps if r.ready), reps[0] if reps else None)
            if not target:
                return 127
            return int(reconciler._runtime.exec(target.pod_name, cmd, timeout=timeout))

        import logging, errno

        def _system_info():
            # Compose a lightweight snapshot for the demo dashboard
            try:
                statuses = store.list_status()
            except Exception:
                statuses = []
            # Nodes snapshot (heartbeat freshness, cordon)
            _nodes = []
            try:
                import os as _os
                from datetime import datetime as _dt, timezone as _tz

                grace = int(_os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
                now = _dt.now(_tz.utc)
                for node, status in store.list_nodes():
                    seen_at = getattr(status, "seen_at", None)
                    age = None
                    if seen_at:
                        try:
                            age = (now - seen_at).total_seconds()
                        except Exception:
                            age = None
                    st = str(status.status if status else "unknown").lower()
                    stale = False
                    if age is not None and age > grace:
                        stale = True
                        if st == "ready":
                            st = "notready"
                    _nodes.append(
                        {
                            "id": node.node_id,
                            "name": node.name,
                            "labels": node.labels,
                            "taints": node.taints,
                            "backend": node.backend,
                            "endpoint": node.endpoint,
                            "pod_cidr": node.pod_cidr,
                            "cordoned": bool(getattr(node, "cordoned", False)),
                            "status": st,
                            "stale": stale,
                            "last_seen_seconds": age,
                        }
                    )
            except Exception:
                _nodes = []

            # Placements: map pod_name -> node for dashboard
            placements: dict[str, list[dict]] = {}
            for s in statuses:
                try:
                    repl_nodes = {
                        row[0]: row[1] for row in store.list_pod_nodes(s.app_name)
                    }
                except Exception:
                    repl_nodes = {}
                try:
                    reps = store.list_pods(s.app_name)
                except Exception:
                    reps = []
                entries = []
                for r in reps:
                    entries.append(
                        {
                            "pod_name": r.pod_name,
                            "replica_id": r.pod_name,
                            "node_id": repl_nodes.get(r.pod_name),
                            "ready": bool(r.ready),
                            "live": bool(r.live),
                            "status": r.status,
                        }
                    )
                placements[s.app_name] = entries

            # Ingress snapshot (paths exist only if manager is configured)
            ing = None
            try:
                ingress = reconciler._ingress_service  # type: ignore[attr-defined]
                if ingress is not None:
                    sites = []
                    try:
                        for s in statuses:
                            try:
                                p = ingress._manager._site_path(s.app_name)  # type: ignore[attr-defined]
                                sites.append(
                                    {
                                        "app": s.app_name,
                                        "host": s.ingress_host,
                                        "path": str(p),
                                        "exists": p.exists(),
                                    }
                                )
                            except Exception:
                                sites.append(
                                    {
                                        "app": s.app_name,
                                        "host": s.ingress_host,
                                        "path": None,
                                        "exists": False,
                                    }
                                )
                    except Exception:
                        sites = []
                    ing = {"dirty": bool(getattr(ingress, "_dirty", False)), "sites": sites}
            except Exception:
                ing = None

            # Services (declared) from manifests
            services = []
            for s in statuses:
                try:
                    man = store.get_revision_manifest(s.app_name, s.revision)
                    svc = getattr(man.spec, "service", None)
                    if svc is not None:
                        services.append(
                            {
                                "app": s.app_name,
                                "port": getattr(svc, "port", None),
                                "target_port": getattr(svc, "target_port", None),
                                "replicas": s.desired_replicas,
                            }
                        )
                except Exception:
                    continue

            # Runtime snapshots
            try:
                runtime = reconciler._runtime  # type: ignore[attr-defined]
            except Exception:
                runtime = None
            containers = []
            volumes = []
            if runtime is not None:
                try:
                    containers = runtime.list_containers_info() or []  # type: ignore[attr-defined]
                except Exception:
                    containers = []
                try:
                    volumes = runtime.list_storage_volumes() or []  # type: ignore[attr-defined]
                except Exception:
                    volumes = []
            # Overlay health (WireGuard)
            ov = None
            try:
                from ae.network.overlay_health import wireguard_health

                ov = wireguard_health()
            except Exception:
                ov = None

            # Create cooldowns (seconds remaining) if available
            cooldowns = {}
            try:
                cd_map = getattr(reconciler, "_create_cooldown_until", {})  # type: ignore[attr-defined]
                import time as _t

                now = float(_t.time())
                for app, until in (cd_map or {}).items():
                    rem = max(0, int(float(until) - now))
                    if rem > 0:
                        cooldowns[str(app)] = rem
            except Exception:
                cooldowns = {}

            # Docs service health (best effort)
            docs = None
            try:
                import os as _os, urllib.request as _ur

                dport = int(_os.getenv("AE_DOCS_PORT", "9109") or 9109)
                url = f"http://127.0.0.1:{dport}/"
                ok = False
                try:
                    with _ur.urlopen(url, timeout=1) as r:  # noqa: S310 - local health probe
                        ok = int(getattr(r, "status", 200)) >= 200
                except Exception:
                    ok = False
                docs = {"port": dport, "ok": bool(ok)}
            except Exception:
                docs = None

            # API health (self-check via /health when port configured)
            api = None
            try:
                import urllib.request as _ur

                port = int(args.metrics_port or 0)
                if port > 0:
                    url = f"http://127.0.0.1:{port}/health"
                    ok = False
                    try:
                        with _ur.urlopen(url, timeout=1) as r:  # noqa: S310 - local self-check
                            ok = int(getattr(r, "status", 200)) >= 200
                    except Exception:
                        ok = False
                    api = {"port": port, "ok": bool(ok)}
            except Exception:
                api = None

            # Return combined snapshot
            return {
                "ingress": ing,
                "services": services,
                "service_endpoints": {
                    s.app_name: {
                        "total": len(store.list_service_endpoints(s.app_name)),
                        "ready": sum(
                            1 for e in store.list_service_endpoints(s.app_name) if e.ready
                        ),
                    }
                    for s in store.list_services()
                },
                "nodes": _nodes,
                "placements": placements,
                "containers": containers,
                "volumes": volumes,
                "overlay": ov,
                "cooldown": cooldowns,
                "docs": docs,
                "api": api,
            }

        try:
            # Planner: reuse CLI planner logic for diagnostics and host-port checks
            def _plan(payload: dict) -> dict:  # noqa: ANN001
                from ae.controller.spec import AppManifest
                from ae.ingress.tls_sync import TlsSecretResolver
                import os as _os

                # Validate manifest
                manifest = AppManifest.model_validate(payload)
                desired = int(manifest.spec.replicas)
                rollout = getattr(manifest.spec, "rollout", {}) or {}
                svc = getattr(manifest.spec, "service", None)
                warnings: list[str] = []
                diagnostics: dict = {"service": {}, "tls": {}}
                # NodePort validation and duplicates
                if svc and getattr(svc, "type", None) == "NodePort" and getattr(svc, "ports", None):
                    NP_MIN, NP_MAX = 30000, 32767
                    name_seen: set[str] = set()
                    port_seen: set[int] = set()
                    nodeport_seen: set[int] = set()
                    dup_names: list[str] = []
                    dup_ports: list[int] = []
                    dup_nps: list[int] = []
                    oor: list[int] = []
                    for sp in svc.ports:
                        np = getattr(sp, "node_port", None)
                        if np is not None and not (NP_MIN <= int(np) <= NP_MAX):
                            warnings.append(
                                f"service.ports[{getattr(sp, 'name', '')}].nodePort {np} is outside the default Kubernetes range 30000-32767"
                            )
                            oor.append(int(np))
                        nm = getattr(sp, "name", None)
                        if nm in name_seen:
                            warnings.append(f"duplicate service port name '{nm}'")
                            dup_names.append(str(nm))
                        elif nm is not None:
                            name_seen.add(nm)
                        try:
                            pnum = int(getattr(sp, "port", -1))
                            if pnum in port_seen:
                                warnings.append(f"duplicate service port {pnum}")
                                dup_ports.append(pnum)
                            else:
                                port_seen.add(pnum)
                        except Exception:
                            pass
                        if np is not None:
                            npi = int(np)
                            if npi in nodeport_seen:
                                warnings.append(f"duplicate service nodePort {npi}")
                                dup_nps.append(npi)
                            else:
                                nodeport_seen.add(npi)
                    diagnostics["service"]["duplicates"] = {
                        "names": dup_names,
                        "ports": dup_ports,
                        "nodePorts": dup_nps,
                    }
                    diagnostics["service"]["outOfRangeNodePorts"] = oor
                # Stable host port conflicts (single-replica only)
                conflicts: dict[int, list[str]] = {}
                if svc and desired == 1:
                    ports_to_check: list[int] = []
                    if getattr(svc, "ports", None):
                        try:
                            ports_to_check = [
                                int(sp.port)
                                for sp in svc.ports
                                if getattr(sp, "port", None) is not None
                            ]
                        except Exception:
                            ports_to_check = []
                    elif getattr(svc, "port", None) is not None:
                        ports_to_check = [int(svc.port)]
                    if ports_to_check:
                        try:
                            infos = reconciler._runtime.list_containers_info()  # type: ignore[attr-defined]
                        except Exception:
                            infos = []
                        for p in ports_to_check:
                            for info in infos or []:
                                if p in (info.get("host_ports") or []):
                                    conflicts.setdefault(int(p), []).append(
                                        str(info.get("name", ""))
                                    )
                        if any(conflicts.values()):
                            diagnostics["service"]["hostPortConflicts"] = conflicts
                # TLS check
                ing = getattr(manifest.spec, "ingress", None)
                if ing and getattr(ing, "tls", True) and getattr(ing, "tls_secret_name", None):
                    root = _os.getenv("AE_TLS_DIR", "state/tls")
                    res = TlsSecretResolver(Path(root)).resolve(str(ing.tls_secret_name))
                    diagnostics["tls"] = {
                        "ingress": True,
                        "secretName": str(ing.tls_secret_name),
                        "root": root,
                        "resolved": bool(res),
                        **({"cert": str(res[0]), "key": str(res[1])} if res else {}),
                    }
                    if res is None:
                        warnings.append(
                            f"ingress.tlsSecretName '{ing.tls_secret_name}' not found under AE_TLS_DIR={root}; controller will fall back to Caddy 'tls internal'"
                        )
                return {
                    "app": app_key_for_manifest(manifest),
                    "replicas": desired,
                    "rollout": {
                        "strategy": str(rollout.get("strategy", "parallel")),
                        "maxSurge": rollout.get("maxSurge", 1),
                        "maxUnavailable": rollout.get("maxUnavailable", 0),
                    },
                    "service": (
                        {
                            "port": getattr(svc, "port", None),
                            "targetPort": getattr(svc, "target_port", None),
                        }
                        if svc
                        else None
                    ),
                    "warnings": warnings,
                    "diagnostics": diagnostics,
                    "ok": len(warnings) == 0,
                }

            def _rollout_pause(app: str) -> dict:
                revs = store.list_revisions(app, limit=1)
                if not revs:
                    raise RuntimeError(f"no revisions recorded for {app}")
                man = store.get_revision_manifest(app, revs[0].revision)
                ro = dict(getattr(man.spec, "rollout", {}) or {})
                ro["pause"] = True
                new_spec = man.spec.model_copy(update={"rollout": ro})
                updated = man.model_copy(update={"spec": new_spec})
                try:
                    existing = store.get_registered_entry(app)
                    src = existing.source if existing else "api"
                    lbls = existing.labels if existing else None
                    store.register_app(updated, source=src, labels=lbls)
                except Exception:
                    pass
                report = reconciler.reconcile(updated)
                # Best-effort fast-follow burst to surface "paused" promptly
                try:
                    import os as _os, time as _t

                    burst = int(_os.getenv("AE_ROLLOUT_RECONCILE_BURST", "2"))
                    delay_ms = int(_os.getenv("AE_ROLLOUT_RECONCILE_DELAY_MS", "300"))
                    burst = max(0, burst)
                    for _ in range(burst):
                        if str(report.revision_status).lower() == "paused":
                            break
                        _t.sleep(max(0.001, delay_ms / 1000.0))
                        report = reconciler.reconcile(updated)
                except Exception:
                    pass
                return {"app": app, "revision": report.revision, "status": report.revision_status}

            def _rollout_resume(app: str) -> dict:
                revs = store.list_revisions(app, limit=1)
                if not revs:
                    raise RuntimeError(f"no revisions recorded for {app}")
                man = store.get_revision_manifest(app, revs[0].revision)
                ro = dict(getattr(man.spec, "rollout", {}) or {})
                ro["pause"] = False
                new_spec = man.spec.model_copy(update={"rollout": ro})
                updated = man.model_copy(update={"spec": new_spec})
                try:
                    existing = store.get_registered_entry(app)
                    src = existing.source if existing else "api"
                    lbls = existing.labels if existing else None
                    store.register_app(updated, source=src, labels=lbls)
                except Exception:
                    pass
                report = reconciler.reconcile(updated)
                try:
                    store.record_event(app, report.revision, "RolloutResumed", "Rollout resumed")
                except Exception:
                    pass
                # Best-effort fast-follow burst to surface "ready" promptly
                try:
                    import os as _os, time as _t

                    burst = int(_os.getenv("AE_ROLLOUT_RECONCILE_BURST", "2"))
                    delay_ms = int(_os.getenv("AE_ROLLOUT_RECONCILE_DELAY_MS", "300"))
                    burst = max(0, burst)
                    for _ in range(burst):
                        if str(report.revision_status).lower() == "ready":
                            break
                        _t.sleep(max(0.001, delay_ms / 1000.0))
                        report = reconciler.reconcile(updated)
                except Exception:
                    pass
                return {"app": app, "revision": report.revision, "status": report.revision_status}

            api_server, assigned, _ = start_http_api(
                args.metrics_port,
                store,
                scale_fn=_scale,
                delete_fn=_delete,
                apply_fn=_apply,
                exec_fn=_exec,
                logs_fn=_logs,
                system_info_fn=_system_info,
                plan_fn=_plan,
                rollout_pause_fn=_rollout_pause,
                rollout_resume_fn=_rollout_resume,
            )
            logging.getLogger(__name__).info("http api listening on port %s", assigned)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EADDRINUSE:
                logging.getLogger(__name__).error(
                    "http api port %s already in use; exiting so supervisor can restart",
                    args.metrics_port,
                )
                return 2
            raise

    if args.once:
        try:
            _import_specs(specs_dir, store, source="specs")
            _import_edge_ingress_specs(specs_dir, store, source="specs")
            _reconcile_edge_ingress(store, _edge_renderer)
        except Exception:
            pass
        try:
            _sync_apishim_registry(store, reconciler)
        except Exception:
            pass
        try:
            entries = store.list_registered_apps()
        except Exception:
            entries = []
        _reconcile_all(reconciler, [entry.manifest for entry in entries])
        return 0

    # loop mode
    stop = False
    try:
        heartbeat_grace = int(os.getenv("AE_NODE_NOTREADY_AFTER", "40") or 40)
    except Exception:
        heartbeat_grace = 40
    heartbeat_interval = max(5, min(20, max(1, heartbeat_grace // 2)))
    last_heartbeat = 0.0

    def _graceful(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    # optional filesystem watch
    changed = True  # force initial reconcile
    observer = None
    last_full = 0.0
    if args.watch:
        try:
            from watchdog.observers import Observer  # type: ignore
            from watchdog.events import FileSystemEventHandler  # type: ignore

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):  # noqa: D401 - event callback
                    nonlocal changed
                    # Only react to YAML-like files
                    import os

                    _, ext = os.path.splitext(getattr(event, "src_path", ""))
                    if ext.lower() in {".yml", ".yaml"}:
                        changed = True

            observer = Observer()
            handler = Handler()
            observer.schedule(handler, str(specs_dir), recursive=True)
            observer.start()
            import logging

            logging.getLogger(__name__).info(
                "watching %s for changes (debounce=%sms)", specs_dir, args.debounce_ms
            )
        except Exception:
            observer = None  # fallback to interval polling
            import logging

            logging.getLogger(__name__).info(
                "watchdog not available; falling back to interval polling"
            )
    else:
        import logging

        logging.getLogger(__name__).info("polling every %ss (no file watch)", args.interval)

    try:
        while not stop:
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                try:
                    store.record_heartbeat(_local_node_id(), "Ready")
                except Exception:
                    pass
                else:
                    last_heartbeat = now
            do_full = changed or (now - last_full) >= max(1, int(args.interval))
            if do_full:
                t0 = time.time()
                try:
                    _import_specs(specs_dir, store, source="specs")
                    _import_edge_ingress_specs(specs_dir, store, source="specs")
                    _reconcile_edge_ingress(store, _edge_renderer)
                except Exception:
                    pass
                try:
                    _sync_apishim_registry(store, reconciler)
                except Exception:
                    pass
                try:
                    entries = store.list_registered_apps()
                except Exception:
                    entries = []
                _reconcile_all(reconciler, [entry.manifest for entry in entries])
                t1 = time.time()
                set_reconcile_metrics(ts_seconds=t1, duration_seconds=(t1 - t0))
                last_full = now
                # debounce
                changed = False
                time.sleep(max(0.001, args.debounce_ms / 1000.0))
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if api_server is not None:
            api_server.shutdown()
            api_server.server_close()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=1)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
