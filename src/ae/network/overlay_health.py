"""Best-effort overlay/WireGuard health probe."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def wireguard_health(iface: str = "wg0") -> dict:
    """Return peer count, latest handshake age (seconds), and MTU for the iface."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", iface):
        return {
            "ok": False,
            "error": "invalid iface",
            "peers": 0,
            "latest_handshake_seconds": None,
            "mtu": None,
            "rosenpass": _rosenpass_status(),
        }
    wg_bin = shutil.which("wg") or "wg"
    ip_bin = shutil.which("ip") or "ip"
    try:
        out = subprocess.check_output(  # noqa: S603
            [wg_bin, "show", iface],  # noqa: S603
            text=True,  # noqa: S603,S607 - fixed argv; iface validated; shell disabled
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return {
            "ok": False,
            "peers": 0,
            "latest_handshake_seconds": None,
            "mtu": None,
            "rosenpass": _rosenpass_status(),
        }
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
        ip_out = subprocess.check_output(  # noqa: S603
            [ip_bin, "link", "show", iface],  # noqa: S603
            text=True,  # noqa: S603,S607 - fixed argv; iface validated; shell disabled
            stderr=subprocess.DEVNULL,
        )
        m = re.search(r"mtu (\d+)", ip_out)
        if m:
            mtu = int(m.group(1))
    except Exception:
        mtu = None
    return {
        "ok": peers > 0,
        "peers": peers,
        "latest_handshake_seconds": latest,
        "mtu": mtu,
        "rosenpass": _rosenpass_status(),
    }


def _rosenpass_status() -> dict | None:
    raw = os.getenv("AE_ROSENPASS_STATUS_PATH")
    if raw:
        path = Path(raw)
    else:
        base = os.getenv("AE_ROSENPASS_DIR", "/var/lib/ae/rosenpass")
        path = Path(base) / "rosenpass-status.json"
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
