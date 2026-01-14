"""Best-effort overlay/WireGuard health probe."""

from __future__ import annotations

import re
import shutil
import subprocess


def wireguard_health(iface: str = "wg0") -> dict:
    """Return peer count, latest handshake age (seconds), and MTU for the iface."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", iface):
        return {
            "ok": False,
            "error": "invalid iface",
            "peers": 0,
            "latest_handshake_seconds": None,
            "mtu": None,
        }
    wg_bin = shutil.which("wg") or "wg"
    ip_bin = shutil.which("ip") or "ip"
    try:
        out = subprocess.check_output(
            [wg_bin, "show", iface], text=True  # noqa: S603,S607 - fixed argv; iface validated; shell disabled
        )
    except Exception:
        return {"ok": False, "peers": 0, "latest_handshake_seconds": None, "mtu": None}
    peers = len(re.findall(r"peer: ", out))
    latest = None
    for line in out.splitlines():
        if "latest handshake" in line:
            match = re.search(r"(\d+) seconds", line)
            if not match:
                continue
            sec = int(match.group(1))
            latest = sec if latest is None else min(latest, sec)
    mtu = None
    try:
        ip_out = subprocess.check_output(
            [ip_bin, "link", "show", iface], text=True  # noqa: S603,S607 - fixed argv; iface validated; shell disabled
        )
        m = re.search(r"mtu (\d+)", ip_out)
        if m:
            mtu = int(m.group(1))
    except Exception:
        mtu = None
    return {"ok": peers > 0, "peers": peers, "latest_handshake_seconds": latest, "mtu": mtu}
