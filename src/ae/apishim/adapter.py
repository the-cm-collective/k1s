from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ae.controller.spec import (
    AppManifest,
    AppSpec,
    Metadata,
    PortSpec,
    ServiceSpec,
    IngressSpec,
)
from ae.controller.reconciler import Reconciler
from ae.controller.state import SQLiteStateStore
from ae.runtime import StubRuntime, DockerRuntime, PodmanRuntime, RuntimeAdapter

from .store import ObjectStore, K8sObject


def _app_name(ns: Optional[str], name: str) -> str:
    return f"{ns}--{name}" if ns else name


def _manifest_from_deployment(
    dep: K8sObject,
    *,
    service_spec: ServiceSpec | None = None,
    ingress_spec: IngressSpec | None = None,
) -> AppManifest:
    spec: Dict[str, Any] = dep.spec or {}
    tpl = ((spec.get("template") or {}).get("spec") or {})
    containers = tpl.get("containers") or []
    if not containers:
        # Minimal placeholder to satisfy schema; image required
        image = "busybox:latest"
        ports: list[PortSpec] = []
    else:
        c0 = containers[0]
        image = c0.get("image") or "busybox:latest"
        ports = []
        for p in c0.get("ports") or []:
            try:
                port_num = int(p.get("containerPort"))
            except Exception:
                continue
            name = p.get("name") or f"p{port_num}"
            ports.append(PortSpec(name=name, containerPort=port_num))

    replicas = int(spec.get("replicas", 1) or 1)
    # Ensure >=1 for manifest schema; scale-to-0 handled by adapter
    m_replicas = max(1, replicas)

    app_spec = AppSpec(image=image, replicas=m_replicas, ports=ports)
    if service_spec is not None:
        app_spec = app_spec.model_copy(update={"service": service_spec})
    if ingress_spec is not None:
        app_spec = app_spec.model_copy(update={"ingress": ingress_spec})
    meta = Metadata(name=_app_name(dep.namespace, dep.name))
    return AppManifest(apiVersion="ae.dev/v1alpha1", kind="App", metadata=meta, spec=app_spec)


def _runtime_from_env() -> RuntimeAdapter:
    backend = (os.getenv("AE_APISHIM_RUNTIME") or os.getenv("AE_RUNTIME_BACKEND") or "stub").lower()
    if backend in {"stub", "test"}:
        return StubRuntime()
    if backend in {"podman", "oci"}:
        try:
            return PodmanRuntime()
        except Exception:
            return DockerRuntime()
    return DockerRuntime()


class AdapterWorker(threading.Thread):
    """Watches apps/v1 Deployments and reconciles into k1s via Reconciler.

    For replicas=0, removes app instead of reconciling (k1s schema requires replicas>=1).
    """

    daemon = True

    def __init__(self, store: ObjectStore, state_store: SQLiteStateStore, reconciler: Reconciler) -> None:
        super().__init__(name="apishim-adapter")
        self._store = store
        self._state = state_store
        self._reconciler = reconciler
        self._stop = threading.Event()
        self._service_specs: dict[tuple[Optional[str], str], ServiceSpec] = {}
        self._ingress_specs: dict[tuple[Optional[str], str], IngressSpec] = {}
        self._service_name_map: dict[tuple[Optional[str], str], tuple[Optional[str], str]] = {}
        self._ingress_owner_map: dict[tuple[Optional[str], str], tuple[Optional[str], str]] = {}
        self._lock = threading.RLock()
        self._service_thread: threading.Thread | None = None
        self._ingress_thread: threading.Thread | None = None
        self._port_file = Path(
            os.getenv("AE_APISHIM_PORT_STATE", "state/apishim_service_ports.json")
        )
        self._port_file.parent.mkdir(parents=True, exist_ok=True)
        self._port_assignments: dict[str, dict[str, int]] = {}
        self._used_ports: set[int] = set()
        self._port_low = int(os.getenv("AE_APISHIM_NODEPORT_MIN", "31000"))
        self._port_high = int(os.getenv("AE_APISHIM_NODEPORT_MAX", "32767"))
        self._load_port_assignments()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._service_thread and self._service_thread.is_alive():
                self._service_thread.join(timeout=0.2)
        except Exception:
            pass
        try:
            if self._ingress_thread and self._ingress_thread.is_alive():
                self._ingress_thread.join(timeout=0.2)
        except Exception:
            pass

    def run(self) -> None:
        self._service_thread = threading.Thread(target=self._watch_services, daemon=True)
        self._ingress_thread = threading.Thread(target=self._watch_ingresses, daemon=True)
        self._service_thread.start()
        self._ingress_thread.start()
        gen = self._store.watch(
            "apps", "v1", "deployments", None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                if ev in {"ADDED", "MODIFIED"}:
                    self._apply_deployment(obj)
        finally:
            self._stop.set()
            try:
                gen.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _apply_deployment(self, dep: K8sObject) -> None:
        spec = dep.spec or {}
        desired = int(spec.get("replicas", 1) or 1)
        app_name = _app_name(dep.namespace, dep.name)
        if desired <= 0:
            # Scale-to-0: call runtime remove and update status to zero
            try:
                # Reconciler's runtime may have remove_app
                self._reconciler._runtime.remove_app(app_name)  # type: ignore[attr-defined]
            except Exception:
                pass
            # Update synthesized status to zeros
            st = {"replicas": 0, "updatedReplicas": 0, "readyReplicas": 0, "availableReplicas": 0,
                  "conditions": [{"type": "Available", "status": "False", "reason": "ScaledDown"},
                                  {"type": "Progressing", "status": "False", "reason": "ScaledDown"}]}
            self._store.upsert("apps", "v1", "deployments", dep.namespace, dep.name, dep.metadata, dep.spec, status=st)
            return

        dep_key = (dep.namespace, dep.name)
        svc_spec = self._service_specs.get(dep_key)
        ing_spec = self._ingress_specs.get(dep_key)
        m = _manifest_from_deployment(dep, service_spec=svc_spec, ingress_spec=ing_spec)
        report = self._reconciler.reconcile(m)
        # Reflect status from state store
        st_row = self._state.get_status(m.metadata.name)
        if st_row is not None:
            st = {
                "replicas": st_row.desired_replicas,
                "updatedReplicas": st_row.live_replicas,
                "readyReplicas": st_row.ready_replicas,
                "availableReplicas": st_row.ready_replicas,
                "conditions": [
                    {
                        "type": "Available",
                        "status": "True" if st_row.ready_replicas >= st_row.desired_replicas else "False",
                        "reason": "MinimumReplicasAvailable",
                    },
                    {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
                ],
            }
            self._store.upsert(
                "apps", "v1", "deployments", dep.namespace, dep.name, dep.metadata, dep.spec, status=st
            )

    def _trigger_reconcile(self, namespace: Optional[str], deploy_name: str) -> None:
        if namespace is None:
            return
        dep = self._store.get("apps", "v1", "deployments", namespace, deploy_name)
        if dep is None:
            return
        self._apply_deployment(dep)

    def _watch_services(self) -> None:
        gen = self._store.watch(
            "", "v1", "services", None, heartbeat_seconds=5, allow_bookmarks=True
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                self._handle_service_event(ev, obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _watch_ingresses(self) -> None:
        gen = self._store.watch(
            "networking.k8s.io",
            "v1",
            "ingresses",
            None,
            heartbeat_seconds=5,
            allow_bookmarks=True,
        )
        try:
            for ev, obj in gen:
                if self._stop.is_set():
                    break
                self._handle_ingress_event(ev, obj)
        finally:
            try:
                gen.close()
            except Exception:
                pass

    def _handle_service_event(self, ev: str, obj: K8sObject) -> None:
        ns = obj.namespace
        svc_name = obj.name
        if ev == "DELETED":
            dep_key = None
            with self._lock:
                dep_key = self._service_name_map.pop((ns, svc_name), None)
                if dep_key:
                    self._service_specs.pop(dep_key, None)
            self._release_service_ports(f"{ns or ''}/{svc_name}")
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return

        result = self._service_spec_for(obj)
        if not result:
            dep_key = None
            with self._lock:
                dep_key = self._service_name_map.pop((ns, svc_name), None)
                if dep_key:
                    self._service_specs.pop(dep_key, None)
            self._release_service_ports(f"{ns or ''}/{svc_name}")
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        dep_key, svc_spec = result
        with self._lock:
            self._service_specs[dep_key] = svc_spec
            self._service_name_map[(ns, svc_name)] = dep_key
        self._trigger_reconcile(dep_key[0], dep_key[1])

    def _service_spec_for(
        self, svc: K8sObject
    ) -> Optional[tuple[tuple[Optional[str], str], ServiceSpec]]:
        spec = svc.spec or {}
        selector = spec.get("selector") or {}
        if not selector:
            selector = (spec.get("selector") or {}).get("matchLabels") or {}
        target = (
            selector.get("app")
            or selector.get("app.kubernetes.io/name")
            or svc.metadata.get("labels", {}).get("app")
            or svc.metadata.get("annotations", {}).get("apishim.k1s.dev/app")
            or svc.metadata.get("name")
        )
        if not target:
            return None
        svc_type = spec.get("type", "ClusterIP")
        expose_host = svc_type in {"NodePort", "LoadBalancer"}
        ports_in = self._prepare_service_ports(svc, spec, expose_host)
        if not ports_in:
            return None
        svc_ports: list[ServiceSpec.ServicePort] = []
        for idx, entry in enumerate(ports_in):
            try:
                svc_port = int(entry.get("port"))
            except Exception:
                continue
            node_port = entry.get("nodePort")
            tgt = entry.get("targetPort")
            host_port = int(node_port) if node_port is not None and expose_host else None
            svc_ports.append(
                ServiceSpec.ServicePort(
                    name=entry.get("name") or f"port-{idx}",
                    port=int(host_port or svc_port),
                    target_port=tgt,
                    protocol=entry.get("protocol", "TCP"),
                    node_port=node_port if expose_host else None,
                )
            )
        if not svc_ports:
            return None
        svc_spec = ServiceSpec(
            type=svc_type,
            ports=svc_ports,
            port=svc_ports[0].port if expose_host else None,
            target_port=svc_ports[0].target_port,
            external_ips=spec.get("externalIPs", []),
            session_affinity=spec.get("sessionAffinity"),
        )
        dep_key = (svc.namespace, target)
        return dep_key, svc_spec

    def _prepare_service_ports(
        self, svc: K8sObject, spec: Dict[str, Any], expose_host: bool
    ) -> list[Dict[str, Any]]:
        desired = spec.get("ports") or []
        svc_key = f"{svc.namespace or ''}/{svc.name}"
        if not desired:
            self._release_service_ports(svc_key)
            return []
        seen_ids: set[str] = set()
        prepared: list[Dict[str, Any]] = []
        for idx, entry in enumerate(desired):
            port_entry = dict(entry)
            port_id = str(port_entry.get("name") or f"idx-{idx}")
            seen_ids.add(port_id)
            node_port = port_entry.get("nodePort")
            if expose_host:
                if node_port is None:
                    node_port = self._allocate_node_port(svc_key, port_id)
                    port_entry["nodePort"] = node_port
                else:
                    try:
                        node_port = int(node_port)
                    except Exception:
                        node_port = self._allocate_node_port(svc_key, port_id)
                    self._reserve_node_port(svc_key, port_id, node_port)
                port_entry["port"] = node_port
            else:
                self._release_port_assignment(svc_key, port_id)
            prepared.append(port_entry)
        self._cleanup_unused_ports(svc_key, seen_ids)
        return prepared

    def _cleanup_unused_ports(self, svc_key: str, keep: set[str]) -> None:
        with self._lock:
            existing = self._port_assignments.get(svc_key)
            if not existing:
                return
            stale = [pid for pid in existing if pid not in keep]
            for pid in stale:
                port = existing.pop(pid, None)
                if port is not None:
                    self._used_ports.discard(port)
            if not existing:
                self._port_assignments.pop(svc_key, None)
            self._save_port_assignments()

    def _allocate_node_port(self, svc_key: str, port_id: str) -> int:
        with self._lock:
            existing = self._port_assignments.setdefault(svc_key, {})
            if port_id in existing:
                return existing[port_id]
            for candidate in range(self._port_low, self._port_high + 1):
                if candidate not in self._used_ports:
                    existing[port_id] = candidate
                    self._used_ports.add(candidate)
                    self._save_port_assignments()
                    return candidate
        raise RuntimeError("No available nodePort in configured range")

    def _reserve_node_port(self, svc_key: str, port_id: str, port: int) -> None:
        with self._lock:
            self._port_assignments.setdefault(svc_key, {})[port_id] = port
            self._used_ports.add(port)
            self._save_port_assignments()

    def _release_service_ports(self, svc_key: str) -> None:
        with self._lock:
            ports = self._port_assignments.pop(svc_key, None)
            if ports:
                for port in ports.values():
                    self._used_ports.discard(port)
                self._save_port_assignments()

    def _release_port_assignment(self, svc_key: str, port_id: str) -> None:
        with self._lock:
            ports = self._port_assignments.get(svc_key)
            if not ports:
                return
            port = ports.pop(port_id, None)
            if port is not None:
                self._used_ports.discard(port)
            if not ports:
                self._port_assignments.pop(svc_key, None)
            self._save_port_assignments()

    def _load_port_assignments(self) -> None:
        if not self._port_file.exists():
            return
        try:
            data = json.loads(self._port_file.read_text())
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for svc_key, ports in data.items():
            if not isinstance(ports, dict):
                continue
            normalized: dict[str, int] = {}
            for pid, port in ports.items():
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    continue
                normalized[str(pid)] = port
                self._used_ports.add(port)
            if normalized:
                self._port_assignments[svc_key] = normalized

    def _save_port_assignments(self) -> None:
        try:
            tmp = self._port_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._port_assignments))
            tmp.replace(self._port_file)
        except Exception:
            pass

    def _handle_ingress_event(self, ev: str, obj: K8sObject) -> None:
        if ev == "DELETED":
            dep_key = None
            with self._lock:
                dep_key = self._ingress_owner_map.pop((obj.namespace, obj.name), None)
                if dep_key:
                    self._ingress_specs.pop(dep_key, None)
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        result = self._ingress_spec_for(obj)
        if not result:
            dep_key = None
            with self._lock:
                dep_key = self._ingress_owner_map.pop((obj.namespace, obj.name), None)
                if dep_key:
                    self._ingress_specs.pop(dep_key, None)
            if dep_key:
                self._trigger_reconcile(dep_key[0], dep_key[1])
            return
        dep_key, ingress_spec = result
        with self._lock:
            self._ingress_specs[dep_key] = ingress_spec
            self._ingress_owner_map[(obj.namespace, obj.name)] = dep_key
        self._trigger_reconcile(dep_key[0], dep_key[1])

    def _ingress_spec_for(
        self, ing: K8sObject
    ) -> Optional[tuple[tuple[Optional[str], str], IngressSpec]]:
        spec = ing.spec or {}
        rules = spec.get("rules") or []
        tls_entries = spec.get("tls") or []
        target_key = None
        host = None
        path = "/"
        for rule in rules:
            rule_host = rule.get("host")
            http = rule.get("http") or {}
            for path_entry in http.get("paths", []):
                backend = path_entry.get("backend", {}).get("service", {})
                svc_name = backend.get("name")
                if not svc_name:
                    continue
                key = self._service_name_map.get((ing.namespace, svc_name))
                if key:
                    target_key = key
                    host = rule_host
                    path = path_entry.get("path") or "/"
                    break
            if target_key:
                break
        if not target_key:
            return None
        tls_secret = None
        tls_enabled = False
        if host:
            for entry in tls_entries:
                hosts = entry.get("hosts", []) or []
                if host in hosts:
                    tls_enabled = True
                    tls_secret = entry.get("secretName")
                    break
        ingress_spec = IngressSpec(
            host=host or "",
            path=path,
            tls=tls_enabled,
            tlsSecretName=tls_secret,
        )
        return target_key, ingress_spec


def build_adapter(store: ObjectStore, runtime: RuntimeAdapter | None = None) -> AdapterWorker:
    db_path = os.getenv("AE_STATE_DB", "state/controller.db")
    state = SQLiteStateStore(Path(db_path))  # type: ignore[name-defined]
    runtime = runtime or _runtime_from_env()
    # Minimal reconciler wiring; skip ingress/secrets/config extras for MVP
    from ae.controller.health import HealthManager

    reconciler = Reconciler(runtime=runtime, state_store=state, health_manager=HealthManager())
    return AdapterWorker(store, state, reconciler)
