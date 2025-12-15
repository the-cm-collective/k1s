"""Docker bridge provider for Service VIPs (Phase 1 stub).

This provider focuses on single-node Service IP allocation and network creation.
Dataplane (proxy) wiring is intentionally minimal; it will be expanded in Phase 1/2.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import tempfile
from typing import Dict, List, Tuple

from ae.controller.state import SQLiteStateStore

from .provider import NetworkProvider


class DockerBridgeProvider(NetworkProvider):
    """Ensure a bridge network exists and allocate ClusterIP addresses."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        network_name: str = "ae-net",
        network_subnet: str | None = None,
        service_cidr: str = "10.241.0.0/16",
        proxy_image: str = "haproxy:2.9-alpine",
        docker_bin: str | None = None,
    ) -> None:
        self._store = store
        self._network_name = network_name
        self._network_subnet = network_subnet or service_cidr
        self._service_cidr = service_cidr
        self._proxy_image = proxy_image
        self._docker = docker_bin or os.getenv("AE_DOCKER_BIN", "docker")

    # NetworkProvider API -------------------------------------------------
    def ensure_network(self) -> None:
        """Ensure the shared bridge network exists."""
        try:
            if self._network_exists():
                return
            self._create_network()
        except Exception:
            # Defensive: do not crash controller if docker is unavailable
            return

    def ensure_service(self, app_name: str, ports: dict) -> str:
        """Allocate/return a ClusterIP for the Service (no proxy yet)."""
        cluster_ip = None
        try:
            existing = self._store.get_service(app_name)
            if existing:
                cluster_ip = existing.cluster_ip
        except Exception:
            cluster_ip = None
        if cluster_ip is None:
            # Allocate the next available IP in the Service CIDR
            cluster_ip = self._allocate_ip()
        # Ensure network exists before wiring proxy
        self.ensure_network()
        # Create or refresh proxy container
        self._ensure_proxy(app_name, cluster_ip, ports)
        return cluster_ip

    def update_service_endpoints(
        self, app_name: str, backends_by_port: Dict[int, List[Tuple[str, int]]]
    ) -> None:
        try:
            cluster_ip = self._store.get_service(app_name).cluster_ip  # type: ignore[union-attr]
        except Exception:
            cluster_ip = None
        cfg = self._render_haproxy(app_name, cluster_ip, backends_by_port)
        name = self._svc_container_name(app_name)
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fp:
                fp.write(cfg)
                tmp = fp.name
            subprocess.run(
                [self._docker, "cp", tmp, f"{name}:/usr/local/etc/haproxy/haproxy.cfg"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        # best-effort reload
        subprocess.run(
            [self._docker, "kill", "-s", "HUP", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def remove_service(self, app_name: str) -> None:
        """Remove proxy container/state (noop for now)."""
        try:
            subprocess.run(
                [self._docker, "rm", "-f", self._svc_container_name(app_name)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    # Internal helpers ---------------------------------------------------
    def _network_exists(self) -> bool:
        try:
            proc = subprocess.run(
                [self._docker, "network", "inspect", self._network_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _create_network(self) -> None:
        args = [
            self._docker,
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            self._network_subnet,
            self._network_name,
        ]
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def _allocate_ip(self) -> str:
        net = ipaddress.ip_network(self._service_cidr, strict=False)
        used = set()
        try:
            for rec in self._store.list_services():
                used.add(ipaddress.ip_address(rec.cluster_ip))
        except Exception:
            pass
        for host in net.hosts():
            if host in used:
                continue
            return str(host)
        raise RuntimeError("Service IP pool exhausted")

    def _svc_container_name(self, app_name: str) -> str:
        return f"ae-svc-{app_name}"

    def _ensure_proxy(self, app_name: str, cluster_ip: str, ports: dict) -> None:
        name = self._svc_container_name(app_name)
        # If container exists, nothing to do (config handled via update)
        try:
            proc = subprocess.run(
                [self._docker, "inspect", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if proc.returncode == 0:
                return
        except Exception:
            pass
        cfg = self._render_haproxy(app_name, cluster_ip, {})
        tmp = f"/tmp/ae-svc-{app_name}.cfg"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                fp.write(cfg)
        except Exception:
            return
        run_args = [
            self._docker,
            "run",
            "-d",
            "--name",
            name,
            "--network",
            self._network_name,
            "--ip",
            cluster_ip,
            "--restart",
            "unless-stopped",
            "-v",
            f"{tmp}:/usr/local/etc/haproxy/haproxy.cfg:ro",
            self._proxy_image,
            "-f",
            "/usr/local/etc/haproxy/haproxy.cfg",
        ]
        subprocess.run(run_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def _render_haproxy(
        self, app_name: str, cluster_ip: str | None, backends: Dict[int, List[Tuple[str, int]]]
    ) -> str:
        lines = [
            "global",
            "  maxconn 10240",
            "defaults",
            "  mode tcp",
            "  timeout connect 5s",
            "  timeout client  50s",
            "  timeout server  50s",
        ]
        for port, servers in sorted(backends.items()):
            lines.append(f"frontend fe_{app_name}_{port}")
            lines.append(f"  bind *:{port}")
            lines.append(f"  default_backend be_{app_name}_{port}")
            lines.append(f"backend be_{app_name}_{port}")
            lines.append("  mode tcp")
            if not servers:
                lines.append("  server placeholder 127.0.0.1:9 check fall 2 rise 3")
            else:
                for idx, (host, target) in enumerate(servers):
                    lines.append(
                        f"  server srv{idx} {host}:{target} check fall 2 rise 3"
                    )
        # Ensure at least one listener to keep haproxy valid when no ports provided
        if not backends:
            lines.extend(
                [
                    f"frontend fe_{app_name}_noop",
                    "  bind *:65535",
                    "  mode tcp",
                    f"  default_backend be_{app_name}_noop",
                    f"backend be_{app_name}_noop",
                    "  mode tcp",
                    "  server placeholder 127.0.0.1:9 check fall 2 rise 3",
                ]
            )
        return "\n".join(lines) + "\n"
