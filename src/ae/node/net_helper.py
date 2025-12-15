"""Best-effort network helper for node agents (Phase 3 overlay plumbing).

This helper is intentionally minimal and designed for lab/demo use. It configures:
- A pod bridge device with the node's Pod CIDR (e.g., 10.42.x.1/24)
- Enables IP forwarding and basic MASQUERADE for pod egress
- Optionally applies a WireGuard configuration supplied via text

All operations are best-effort and require root. Failures are logged but do not
raise, so the agent can continue to run workload APIs even when overlay setup fails.
"""

from __future__ import annotations

import logging
import subprocess

LOGGER = logging.getLogger(__name__)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def ensure_pod_bridge(bridge: str, cidr: str) -> None:
    """Create and configure a pod bridge with the given CIDR."""
    try:
        _run(["ip", "link", "add", bridge, "type", "bridge"])
    except Exception:
        # Already exists or not permitted
        pass
    try:
        _run(["ip", "addr", "flush", "dev", bridge])
        _run(["ip", "addr", "add", f"{cidr}", "dev", bridge])
        _run(["ip", "link", "set", bridge, "up"])
        _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        _run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", cidr, "!", "-d", cidr, "-j", "MASQUERADE"])
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("pod bridge setup failed: %s", exc)


def apply_wireguard(config_text: str, iface: str = "wg0") -> None:
    """Apply a WireGuard config via wg-quick style stdin."""
    try:
        proc = subprocess.run(
            ["wg", "syncconf", iface, "/dev/fd/0"],
            input=config_text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            # Try bringup if iface is missing
            subprocess.run(
                ["wg-quick", "strip", "/dev/fd/0"],
                input=config_text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except FileNotFoundError:
        LOGGER.warning("wireguard tools not installed; skipping")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("wireguard apply failed: %s", exc)

