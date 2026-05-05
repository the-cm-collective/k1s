"""Best-effort network helper for node agents (bridge/NAT/WireGuard).

This helper is intentionally minimal and designed for lab/demo use. It configures:
- A pod bridge device with the node's Pod CIDR (e.g., 10.42.x.1/24)
- Enables IP forwarding and basic MASQUERADE for pod egress
- Optionally applies a WireGuard configuration supplied via text

All operations are best-effort and require root. Failures are logged but do not
raise, so the agent can continue to run workload APIs even when overlay setup fails.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

LOGGER = logging.getLogger(__name__)
IP_BIN = shutil.which("ip") or "ip"
SYSCTL_BIN = shutil.which("sysctl") or "sysctl"
IPTABLES_BIN = shutil.which("iptables") or "iptables"
WG_BIN = shutil.which("wg") or "wg"
WG_QUICK_BIN = shutil.which("wg-quick") or "wg-quick"
LEGACY_POD_BRIDGE = "ae0"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)  # noqa: S603,S607 - fixed binaries; shell disabled


def _ensure_iptables_rule(
    chain: str,
    rule: list[str],
    *,
    table: str | None = None,
    position: int | None = None,
) -> None:
    check_cmd = [IPTABLES_BIN]
    if table:
        check_cmd.extend(["-t", table])
    check_cmd.extend(["-C", chain, *rule])
    result = subprocess.run(  # noqa: S603,S607 - fixed binary; shell disabled
        check_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        return
    apply_cmd = [IPTABLES_BIN]
    if table:
        apply_cmd.extend(["-t", table])
    if position is None:
        apply_cmd.extend(["-A", chain, *rule])
    else:
        apply_cmd.extend(["-I", chain, str(position), *rule])
    _run(apply_cmd)


def _bridge_addr_for_cidr(cidr: str) -> str:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return cidr
    host = next(network.hosts(), network.network_address)
    return f"{host}/{network.prefixlen}"


def _bridge_exists(name: str) -> bool:
    result = subprocess.run(  # noqa: S603,S607 - fixed binary; shell disabled
        [IP_BIN, "link", "show", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def ensure_pod_bridge(bridge: str, cidr: str) -> None:
    """Create and configure a pod bridge with the given CIDR."""
    with contextlib.suppress(Exception):
        if bridge != LEGACY_POD_BRIDGE and _bridge_exists(LEGACY_POD_BRIDGE):
            _run([IP_BIN, "link", "del", LEGACY_POD_BRIDGE])
        _run([IP_BIN, "link", "add", bridge, "type", "bridge"])
    try:
        _run([IP_BIN, "addr", "flush", "dev", bridge])
        _run([IP_BIN, "addr", "add", _bridge_addr_for_cidr(cidr), "dev", bridge])
        _run([IP_BIN, "link", "set", bridge, "up"])
        _run([SYSCTL_BIN, "-w", "net.ipv4.ip_forward=1"])
        _ensure_iptables_rule(
            "FORWARD",
            ["-i", bridge, "-j", "ACCEPT"],
            position=1,
        )
        _ensure_iptables_rule(
            "FORWARD",
            ["-o", bridge, "-j", "ACCEPT"],
            position=1,
        )
        _ensure_iptables_rule(
            "POSTROUTING",
            [
            "-s",
            cidr,
            "!",
            "-d",
            cidr,
            "-j",
            "MASQUERADE",
            ],
            table="nat",
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("pod bridge setup failed: %s", exc)


def apply_wireguard(config_text: str, iface: str = "wg0") -> None:
    """Apply a WireGuard config via wg-quick style stdin."""
    if not str(iface).isalnum() and not str(iface).replace("_", "").replace("-", "").isalnum():
        LOGGER.warning("invalid wg iface: %s", iface)
        return

    def _parse_interface_kv(text: str) -> dict[str, str]:
        """Parse wg-quick-ish [Interface] keys so we can apply IP-level settings.

        `wg syncconf` only touches WireGuard state (keys/peers) and ignores address/MTU/table.
        wg-quick normally applies those via `ip` commands, so we replicate the minimal bits we
        need for lab correctness (addresses + MTU).
        """
        in_iface = False
        out: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                in_iface = line.lower() == "[interface]"
                continue
            if not in_iface or "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key.strip().lower()] = val.strip()
        return out

    def _apply_iface_addrs(addresses_raw: str | None) -> None:
        if not addresses_raw:
            return
        # wg-quick supports comma-separated lists.
        for addr in [a.strip() for a in addresses_raw.split(",") if a.strip()]:
            # `replace` is idempotent and avoids "File exists" failures.
            _run([IP_BIN, "addr", "replace", addr, "dev", iface])

    def _apply_iface_mtu(mtu_raw: str | None) -> None:
        if not mtu_raw:
            return
        try:
            mtu = int(str(mtu_raw).strip())
        except Exception:
            return
        if mtu <= 0:
            return
        _run([IP_BIN, "link", "set", "dev", iface, "mtu", str(mtu)])

    iface_kv = _parse_interface_kv(config_text)
    iface_addr = iface_kv.get("address")
    iface_mtu = iface_kv.get("mtu")

    def _strip_wg_quick(text: str) -> str:
        # wg syncconf rejects wg-quick-only keys (Address, MTU, DNS, etc.).
        drop = {"address", "mtu", "dns", "table", "preup", "postup", "predown", "postdown", "saveconfig"}
        lines = []
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(raw)
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            if key in drop:
                continue
            lines.append(raw)
        return "\n".join(lines).strip() + "\n"
    try:
        proc = subprocess.run(
            [WG_BIN, "syncconf", iface, "/dev/fd/0"],  # noqa: S603,S607 - fixed binary; shell disabled
            input=_strip_wg_quick(config_text),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            if proc.stderr:
                LOGGER.warning("wireguard syncconf failed: %s", proc.stderr.strip())
            # Try bringup if iface is missing.
            with tempfile.TemporaryDirectory() as tmpdir:
                conf_path = Path(tmpdir) / f"{iface}.conf"
                conf_path.write_text(config_text, encoding="utf-8")
                proc2 = subprocess.run(
                    [WG_QUICK_BIN, "up", str(conf_path)],  # noqa: S603,S607 - fixed binary; shell disabled
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc2.returncode != 0 and proc2.stderr:
                    LOGGER.warning("wireguard wg-quick failed: %s", proc2.stderr.strip())
        # Whether syncconf succeeded or we fell back to wg-quick, apply IP-level settings.
        _apply_iface_mtu(iface_mtu)
        _apply_iface_addrs(iface_addr)
        _run([IP_BIN, "link", "set", "dev", iface, "up"])
    except FileNotFoundError:
        LOGGER.warning("wireguard tools not installed; skipping")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("wireguard apply failed: %s", exc)
