"""Iptables-based Service VIP provider (single-node, CRI-friendly)."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import os
import shutil
import subprocess
from typing import Dict, List, Tuple

from ae.controller.state import SQLiteStateStore

from .provider import NetworkProvider

LOGGER = logging.getLogger(__name__)


class IptablesProvider(NetworkProvider):
    """Service VIP provider using iptables NAT rules (kube-proxy style)."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        service_cidr: str = "10.241.0.0/16",
        iptables_bin: str | None = None,
        chain_base: str = "AE-SVC",
    ) -> None:
        self._store = store
        self._service_cidr = service_cidr
        self._iptables = iptables_bin or os.getenv("AE_IPTABLES_BIN", "iptables")
        self._chain_base = chain_base
        self._warned = False
        self._available: bool | None = None

    # NetworkProvider API -------------------------------------------------
    def ensure_network(self) -> None:
        if not self._check_available():
            return
        self._ensure_chain(self._chain_base)
        self._ensure_jump("PREROUTING", self._chain_base, dest=self._service_cidr)
        self._ensure_jump("OUTPUT", self._chain_base, dest=self._service_cidr)

    def ensure_service(self, app_name: str, ports: dict) -> str:
        _ = ports
        cluster_ip = None
        try:
            existing = self._store.get_service(app_name)
            if existing:
                cluster_ip = existing.cluster_ip
        except Exception:
            cluster_ip = None
        if cluster_ip is None:
            cluster_ip = self._allocate_ip()
        self.ensure_network()
        return cluster_ip

    def update_service_endpoints(
        self, app_name: str, backends_by_port: Dict[int, List[Tuple[str, int]]]
    ) -> None:
        if not self._check_available():
            return
        try:
            record = self._store.get_service(app_name)
        except Exception:
            record = None
        if not record:
            return
        cluster_ip = record.cluster_ip
        ports = self._ports_from_record(record.ports)
        if not ports:
            return
        self._ensure_chain(self._chain_base)
        for svc_port, proto in ports.items():
            chain = self._svc_chain(app_name, svc_port)
            self._ensure_chain(chain)
            self._ensure_jump(
                self._chain_base,
                chain,
                dest=f"{cluster_ip}/32",
                dport=svc_port,
                proto=proto,
            )
            self._flush_chain(chain)
            backends = backends_by_port.get(svc_port, [])
            if backends:
                self._add_backend_rules(chain, backends, proto=proto)

    def remove_service(self, app_name: str) -> None:
        if not self._check_available():
            return
        try:
            record = self._store.get_service(app_name)
        except Exception:
            record = None
        if not record:
            return
        cluster_ip = record.cluster_ip
        ports = self._ports_from_record(record.ports)
        for svc_port, proto in ports.items():
            chain = self._svc_chain(app_name, svc_port)
            self._delete_jump(
                self._chain_base,
                chain,
                dest=f"{cluster_ip}/32",
                dport=svc_port,
                proto=proto,
            )
            self._delete_chain(chain)

    # Internal helpers ---------------------------------------------------
    def _check_available(self) -> bool:
        if self._available is not None:
            return bool(self._available)
        iptables_path = shutil.which(self._iptables)
        if iptables_path is None:
            self._available = False
        else:
            try:
                self._available = os.geteuid() == 0
            except Exception:
                self._available = False
        if not self._available and not self._warned:
            self._warned = True
            LOGGER.warning(
                "Iptables Service VIP provider unavailable (requires root + %s on PATH).",
                self._iptables,
            )
        return bool(self._available)

    def _iptables_cmd(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [self._iptables, "-w", "-t", "nat", *args]
        return subprocess.run(  # noqa: S603 - iptables command built from fixed args
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def _ensure_chain(self, name: str) -> None:
        if self._iptables_cmd(["-L", name]).returncode != 0:
            self._iptables_cmd(["-N", name])

    def _flush_chain(self, name: str) -> None:
        self._iptables_cmd(["-F", name])

    def _delete_chain(self, name: str) -> None:
        self._iptables_cmd(["-F", name])
        self._iptables_cmd(["-X", name])

    def _ensure_jump(
        self,
        src_chain: str,
        dest_chain: str,
        *,
        dest: str,
        dport: int | None = None,
        proto: str = "tcp",
    ) -> None:
        rule = ["-d", dest, "-j", dest_chain]
        if dport is not None:
            rule = ["-p", proto.lower(), "--dport", str(int(dport)), *rule]
        check = self._iptables_cmd(["-C", src_chain, *rule])
        if check.returncode != 0:
            self._iptables_cmd(["-A", src_chain, *rule])

    def _delete_jump(
        self,
        src_chain: str,
        dest_chain: str,
        *,
        dest: str,
        dport: int | None = None,
        proto: str = "tcp",
    ) -> None:
        rule = ["-d", dest, "-j", dest_chain]
        if dport is not None:
            rule = ["-p", proto.lower(), "--dport", str(int(dport)), *rule]
        self._iptables_cmd(["-D", src_chain, *rule])

    def _add_backend_rules(
        self,
        chain: str,
        backends: list[tuple[str, int]],
        *,
        proto: str = "tcp",
    ) -> None:
        count = len(backends)
        if count == 0:
            return
        if count == 1:
            host, port = backends[0]
            self._iptables_cmd(
                [
                    "-A",
                    chain,
                    "-p",
                    proto.lower(),
                    "-j",
                    "DNAT",
                    "--to-destination",
                    f"{host}:{int(port)}",
                ]
            )
            return
        for idx, (host, port) in enumerate(backends):
            rule = [
                "-A",
                chain,
                "-p",
                proto.lower(),
                "-j",
                "DNAT",
                "--to-destination",
                f"{host}:{int(port)}",
            ]
            if idx < count - 1:
                prob = 1.0 / float(count - idx)
                rule = [
                    "-A",
                    chain,
                    "-p",
                    proto.lower(),
                    "-m",
                    "statistic",
                    "--mode",
                    "random",
                    "--probability",
                    f"{prob:.5f}",
                    "-j",
                    "DNAT",
                    "--to-destination",
                    f"{host}:{int(port)}",
                ]
            self._iptables_cmd(rule)

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

    def _svc_chain(self, app_name: str, port: int) -> str:
        digest = hashlib.sha1(f"{app_name}:{int(port)}".encode("utf-8")).hexdigest()[:8]
        return f"{self._chain_base}-{digest}"

    def _ports_from_record(self, ports: dict) -> dict[int, str]:
        result: dict[int, str] = {}
        for entry in (ports or {}).get("ports", []) or []:
            try:
                port = int(entry.get("port"))
                proto = str(entry.get("protocol") or "TCP").lower()
                if proto not in {"tcp", "udp"}:
                    proto = "tcp"
                result[port] = proto
            except Exception:
                continue
        return result
