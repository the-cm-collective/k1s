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
import logging
import shutil
import subprocess

LOGGER = logging.getLogger(__name__)
IP_BIN = shutil.which("ip") or "ip"
SYSCTL_BIN = shutil.which("sysctl") or "sysctl"
IPTABLES_BIN = shutil.which("iptables") or "iptables"
WG_BIN = shutil.which("wg") or "wg"
WG_QUICK_BIN = shutil.which("wg-quick") or "wg-quick"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)  # noqa: S603,S607 - fixed binaries; shell disabled


def ensure_pod_bridge(bridge: str, cidr: str) -> None:
    """Create and configure a pod bridge with the given CIDR."""
    with contextlib.suppress(Exception):
        _run([IP_BIN, "link", "add", bridge, "type", "bridge"])
    try:
        _run([IP_BIN, "addr", "flush", "dev", bridge])
        _run([IP_BIN, "addr", "add", f"{cidr}", "dev", bridge])
        _run([IP_BIN, "link", "set", bridge, "up"])
        _run([SYSCTL_BIN, "-w", "net.ipv4.ip_forward=1"])
        iptables_cmd = [
            IPTABLES_BIN,
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            cidr,
            "!",
            "-d",
            cidr,
            "-j",
            "MASQUERADE",
        ]
        _run(iptables_cmd)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("pod bridge setup failed: %s", exc)


def apply_wireguard(config_text: str, iface: str = "wg0") -> None:
    """Apply a WireGuard config via wg-quick style stdin."""
    if not str(iface).isalnum() and not str(iface).replace("_", "").replace("-", "").isalnum():
        LOGGER.warning("invalid wg iface: %s", iface)
        return
    try:
        proc = subprocess.run(
            [WG_BIN, "syncconf", iface, "/dev/fd/0"],  # noqa: S603,S607 - fixed binary; shell disabled
            input=config_text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            # Try bringup if iface is missing
            subprocess.run(
                [WG_QUICK_BIN, "strip", "/dev/fd/0"],  # noqa: S603,S607 - fixed binary; shell disabled
                input=config_text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except FileNotFoundError:
        LOGGER.warning("wireguard tools not installed; skipping")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("wireguard apply failed: %s", exc)
