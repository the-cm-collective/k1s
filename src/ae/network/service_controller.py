"""Service controller that bridges manifests to the network provider."""

from __future__ import annotations

from typing import Dict, List, Tuple

from ae.controller.health import HealthReport
from ae.controller.spec import AppManifest, ServiceSpec
from ae.controller.state import SQLiteStateStore, ServiceEndpoint
from ae.runtime import RuntimeResult

from .provider import NetworkProvider


class ServiceController:
    """Orchestrates Service VIP lifecycle using a NetworkProvider and state store."""

    def __init__(self, provider: NetworkProvider, store: SQLiteStateStore) -> None:
        self._provider = provider
        self._store = store

    def reconcile(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
    ) -> str | None:
        """Ensure Service and endpoints reflect the latest runtime/health state."""

        svc_spec = getattr(manifest.spec, "service", None)
        app = manifest.metadata.name
        if not svc_spec:
            self._cleanup(app)
            return None

        ports = self._render_ports(svc_spec)
        if not ports:
            # No declared Service ports -> nothing to manage
            self._cleanup(app)
            return None

        self._provider.ensure_network()
        cluster_ip = self._provider.ensure_service(app, ports)

        backends = self._build_backends(app, svc_spec, runtime_result, health_report)
        self._store.upsert_service(app, cluster_ip, ports)
        self._store.upsert_service_endpoints(app, backends["records"])
        self._provider.update_service_endpoints(app, backends["by_port"])
        return cluster_ip

    # ---------------- internal helpers ----------------
    def _cleanup(self, app: str) -> None:
        try:
            self._provider.remove_service(app)
        except Exception:
            pass
        try:
            self._store.delete_service(app)
        except Exception:
            pass

    def _render_ports(self, svc: ServiceSpec) -> dict:
        ports: List[dict] = []
        if svc.ports:
            for p in svc.ports:
                try:
                    ports.append(
                        {
                            "name": p.name,
                            "port": int(p.port),
                            "targetPort": int(p.target_port) if p.target_port else int(p.port),
                            "protocol": p.protocol or "TCP",
                        }
                    )
                except Exception:
                    continue
        elif svc.port:
            try:
                ports.append(
                    {
                        "name": "tcp",
                        "port": int(svc.port),
                        "targetPort": int(svc.target_port) if svc.target_port else int(svc.port),
                        "protocol": "TCP",
                    }
                )
            except Exception:
                return {}
        return {"ports": ports}

    def _service_ports(self, svc: ServiceSpec) -> List[Tuple[int, int]]:
        ports: List[Tuple[int, int]] = []
        if svc.ports:
            for p in svc.ports:
                try:
                    sp = int(p.port)
                    tp = int(p.target_port) if p.target_port else sp
                    ports.append((sp, tp))
                except Exception:
                    continue
        elif svc.port:
            try:
                sp = int(svc.port)
                tp = int(svc.target_port) if svc.target_port else sp
                ports.append((sp, tp))
            except Exception:
                pass
        return ports

    def _build_backends(
        self,
        app: str,
        svc: ServiceSpec,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
    ) -> dict:
        ports = self._service_ports(svc)
        if not ports:
            return {"records": [], "by_port": {}}

        states_by_id = {st.replica_id: st for st in runtime_result.replica_states}
        records: List[ServiceEndpoint] = []
        by_port: Dict[int, List[Tuple[str, int]]] = {}
        seen: set[Tuple[int, str, int]] = set()

        for rep in getattr(health_report, "replicas", []) or []:
            if not rep.ready:
                continue
            st = states_by_id.get(rep.replica_id)
            if st is None or not st.endpoint:
                continue
            host, hp = self._split_host_port(st.endpoint)
            if host is None or hp is None:
                continue
            if host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127."):
                # Loopback endpoints are not reachable from proxy containers; skip them.
                continue
            for service_port, _target_port in ports:
                backend_port = _target_port or hp
                key = (service_port, host, backend_port)
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    ServiceEndpoint(
                        app_name=app,
                        port=service_port,
                        ip=host,
                        target_port=backend_port,
                        ready=True,
                    )
                )
                by_port.setdefault(service_port, []).append((host, backend_port))

        return {"records": records, "by_port": by_port}

    def _split_host_port(self, endpoint: str) -> tuple[str | None, int | None]:
        """Parse host:port strings, tolerating IPv6 bracket notation."""
        try:
            if endpoint.startswith("["):
                # [::1]:8080
                host, port = endpoint.rsplit("]:", 1)
                return host.lstrip("["), int(port)
            host, port = endpoint.rsplit(":", 1)
            return host, int(port)
        except Exception:
            return None, None
