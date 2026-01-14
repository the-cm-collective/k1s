"""Overlay-friendly Service provider (Phase 3).

This is a thin adaptation of the Docker bridge provider that:
- Creates/uses a dedicated overlay network name (`AE_OVERLAY_NET`, default `ae-overlay`)
  with the Service CIDR as its subnet.
- Runs a per-Service HAProxy container attached to that network at the allocated
  ClusterIP. Traffic to the VIP must originate from peers on the same overlay
  (e.g., WireGuard-connected nodes bridged into the overlay network namespace).

It is not a full kube-proxy replacement but provides a workable dataplane for
multi-node demos once the overlay is plumbed.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import subprocess
import tempfile

from ae.controller.state import SQLiteStateStore
from ae.network.overlay_health import wireguard_health

from .provider import NetworkProvider


class OverlayProvider(NetworkProvider):
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        network_name: str = "ae-overlay",
        service_cidr: str = "10.241.0.0/16",
        proxy_image: str = "haproxy:2.9-alpine",
        docker_bin: str | None = None,
        manage_network: bool = True,
    ) -> None:
        self._store = store
        self._network_name = network_name
        self._service_cidr = service_cidr
        self._proxy_image = proxy_image
        self._docker = docker_bin or os.getenv("AE_DOCKER_BIN", "docker")
        self._manage_network = manage_network

    # NetworkProvider API -------------------------------------------------
    def ensure_network(self) -> None:
        if not self._manage_network:
            return
        try:
            if self._network_exists():
                return
            self._create_network()
        except Exception:
            return

    def ensure_service(self, app_name: str, ports: dict) -> str:
        with contextlib.suppress(Exception):
            existing = self._store.get_service(app_name)
            if existing:
                return existing.cluster_ip
        cluster_ip = self._allocate_ip()
        self.ensure_network()
        self._ensure_proxy(app_name, cluster_ip, ports)
        return cluster_ip

    def update_service_endpoints(
        self, app_name: str, backends_by_port: dict[int, list[tuple[str, int]]]
    ) -> None:
        try:
            cluster_ip = self._store.get_service(app_name).cluster_ip  # type: ignore[union-attr]
        except Exception:
            cluster_ip = None
        cfg = self._render_haproxy(app_name, cluster_ip, backends_by_port)
        name = self._svc_container_name(app_name)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fp:
                fp.write(cfg)
                tmp = fp.name
            subprocess.run(
                [self._docker, "cp", tmp, f"{name}:/usr/local/etc/haproxy/haproxy.cfg"],  # noqa: S603 - docker CLI; shell disabled
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        finally:
            if tmp:
                with contextlib.suppress(Exception):
                    os.unlink(tmp)
        subprocess.run(
            [self._docker, "kill", "-s", "HUP", name],  # noqa: S603 - docker CLI; shell disabled
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def remove_service(self, app_name: str) -> None:
        with contextlib.suppress(Exception):
            subprocess.run(
                [self._docker, "rm", "-f", self._svc_container_name(app_name)],  # noqa: S603 - docker CLI; shell disabled
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    # Internal helpers ---------------------------------------------------
    def _network_exists(self) -> bool:
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                [self._docker, "network", "inspect", self._network_name],  # noqa: S603 - docker CLI; shell disabled
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return proc.returncode == 0
        return False

    def _create_network(self) -> None:
        args = [
            self._docker,
            "network",
            "create",
            "--driver",
            "bridge",
            "--subnet",
            self._service_cidr,
            self._network_name,
        ]
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)  # noqa: S603

    def _allocate_ip(self) -> str:
        net = ipaddress.ip_network(self._service_cidr, strict=False)
        used = set()
        with contextlib.suppress(Exception):
            for rec in self._store.list_services():
                used.add(ipaddress.ip_address(rec.cluster_ip))
        for host in net.hosts():
            if host in used:
                continue
            return str(host)
        raise RuntimeError("Service IP pool exhausted")

    def _svc_container_name(self, app_name: str) -> str:
        return f"ae-svc-{app_name}"

    def _ensure_proxy(self, app_name: str, cluster_ip: str, _ports: dict) -> None:
        name = self._svc_container_name(app_name)
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                [self._docker, "inspect", name],  # noqa: S603 - docker CLI; shell disabled
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if proc.returncode == 0:
                return
        cfg = self._render_haproxy(app_name, cluster_ip, {})
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fp:
                fp.write(cfg)
                tmp = fp.name
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
            subprocess.run(  # noqa: S603 - docker CLI; shell disabled
                run_args,  # noqa: S603
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        finally:
            if tmp:
                with contextlib.suppress(Exception):
                    os.unlink(tmp)

    def _render_haproxy(
        self, app_name: str, cluster_ip: str | None, backends: dict[int, list[tuple[str, int]]]
    ) -> str:
        _ = cluster_ip  # reserved for future VIP placement logic
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
                    lines.append(f"  server srv{idx} {host}:{target} check fall 2 rise 3")
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

    def overlay_health(self) -> dict:
        """Expose overlay/WireGuard health (best-effort)."""
        try:
            return wireguard_health()
        except Exception:
            return {"ok": False}
