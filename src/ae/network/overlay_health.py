"""Best-effort overlay/WireGuard health probe."""

from __future__ import annotations

import subprocess
import re
import shlex


def wireguard_health(iface: str = "wg0") -> dict:
    """Return peer count, latest handshake age (seconds), and MTU for the iface."""
    try:
        out = subprocess.check_output(["wg", "show", iface], text=True)
    except Exception:
        return {"ok": False, "peers": 0, "latest_handshake_seconds": None, "mtu": None}
    peers = len(re.findall(r"peer: ", out))
    latest = None
    for line in out.splitlines():
        if "latest handshake" in line:
            try:
                sec = int(re.findall(r"(\d+) seconds", line)[0])
                latest = sec if latest is None else min(latest, sec)
            except Exception:
                continue
    mtu = None
    try:
        ip_out = subprocess.check_output(["ip", "link", "show", iface], text=True)
        m = re.search(r"mtu (\d+)", ip_out)
        if m:
            mtu = int(m.group(1))
    except Exception:
        mtu = None
    return {"ok": peers > 0, "peers": peers, "latest_handshake_seconds": latest, "mtu": mtu}
