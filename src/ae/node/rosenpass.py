"""Managed Rosenpass integration for WireGuard PSK rotation (best-effort)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeerConfig:
    name: str
    wg_pubkey: str
    rosenpass_pubkey: str | None
    allowed_ips: list[str]
    endpoint: str | None
    persistent_keepalive: int | None
    role: str | None


@dataclass(frozen=True)
class WireGuardConfig:
    interface: str
    address: str | None
    listen_port: int | None
    mtu: int | None
    table: str | None
    private_key_path: Path
    public_key_path: Path


@dataclass(frozen=True)
class RosenpassConfig:
    private_key_path: Path
    public_key_path: Path
    listen: str | None
    command: list[str] | None
    log_level: str | None


@dataclass(frozen=True)
class RosenpassNodeConfig:
    interface: str
    wireguard: WireGuardConfig
    rosenpass: RosenpassConfig
    peers: list[PeerConfig]
    peers_source: str | None


@dataclass(frozen=True)
class KeyMaterial:
    private_key: str
    public_key: str
    private_path: Path
    public_path: Path


@dataclass(frozen=True)
class RosenpassRuntime:
    interface: str
    wg_config: str | None
    wg_public_key: str | None
    rp_public_key: str | None
    status_path: Path | None


def _expand_path(raw: str | Path | None, base_dir: Path) -> Path:
    if not raw:
        return base_dir
    text = str(raw)
    expanded = os.path.expandvars(os.path.expanduser(text))
    path = Path(expanded)
    if not path.is_absolute():
        return base_dir / path
    return path


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(value)]


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_role(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"initiator", "responder"}:
        return raw
    return None


def _safe_interface(name: Any, default: str = "wg0") -> str:
    raw = str(name or default).strip()
    if not raw:
        return default
    return raw


def _normalize_verbosity(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in {"Quiet", "Verbose"}:
        return text
    low = text.lower()
    if low in {"quiet", "q", "0", "false", "off", "error", "err", "warn", "warning"}:
        return "Quiet"
    if low in {"verbose", "v", "1", "true", "on", "debug", "info", "trace"}:
        return "Verbose"
    return None


def _write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    return data or None


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode_b64(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        return None


def _sanitize_name(value: str, default: str = "peer") -> str:
    raw = value.strip() or default
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


def _peer_has_rosenpass_keys(peers: list[PeerConfig]) -> bool:
    return any(peer.rosenpass_pubkey for peer in peers)


def _write_status_file(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _maybe_dump_wg_config(base_dir: Path, config_text: str) -> None:
    if os.getenv("AE_WG_DEBUG_DUMP", "0") != "1":
        return
    try:
        out = base_dir / "wg-debug.conf"
        out.write_text(config_text, encoding="utf-8")
        os.chmod(out, 0o600)
    except Exception:
        pass


def _derive_address_from_cidr(cidr: str | None) -> str | None:
    if not cidr:
        return None
    try:
        import ipaddress

        net = ipaddress.ip_network(cidr, strict=False)
        first = net.network_address + 1
        return f"{first}/{net.prefixlen}"
    except Exception:
        return None


def _normalize_wg_address(raw: str | None, pod_cidr: str | None) -> str | None:
    if raw is None:
        return _derive_address_from_cidr(pod_cidr)
    text = str(raw).strip()
    if not text:
        return _derive_address_from_cidr(pod_cidr)
    if text.lower() in {"none", "off", "disable", "disabled", "null"}:
        return None
    return text


def load_config(path: Path, *, base_dir: Path | None = None) -> RosenpassNodeConfig:
    base = base_dir or path.parent
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("rosenpass config must be a mapping")
    return _parse_config(data, base)


def load_env_config(base_dir: Path, *, pod_cidr: str | None = None) -> RosenpassNodeConfig:
    wg_address = _normalize_wg_address(os.getenv("AE_WG_ADDRESS"), pod_cidr)
    data: dict[str, Any] = {
        "interface": os.getenv("AE_ROSENPASS_INTERFACE") or os.getenv("AE_WG_INTERFACE") or "wg0",
        "wireguard": {
            "address": wg_address,
            "listen_port": os.getenv("AE_WG_LISTEN_PORT"),
            "private_key_path": os.getenv("AE_WG_PRIVATE_KEY"),
            "public_key_path": os.getenv("AE_WG_PUBLIC_KEY"),
            "mtu": os.getenv("AE_WG_MTU"),
            "table": os.getenv("AE_WG_TABLE"),
        },
        "rosenpass": {
            "private_key_path": os.getenv("AE_ROSENPASS_PRIVATE_KEY"),
            "public_key_path": os.getenv("AE_ROSENPASS_PUBLIC_KEY"),
            "listen": os.getenv("AE_ROSENPASS_LISTEN"),
            "command": os.getenv("AE_ROSENPASS_COMMAND"),
            "log_level": os.getenv("AE_ROSENPASS_LOG_LEVEL"),
        },
        "peers": [],
    }
    return _parse_config(data, base_dir)


def _parse_config(data: dict[str, Any], base_dir: Path) -> RosenpassNodeConfig:
    interface = _safe_interface(data.get("interface"), "wg0")
    wg_data = data.get("wireguard") or {}
    rp_data = data.get("rosenpass") or {}
    peers_source = data.get("peers_source") or data.get("peersSource")

    wg_priv_path = _expand_path(wg_data.get("private_key_path"), base_dir / "wg.key")
    wg_pub_path = _expand_path(wg_data.get("public_key_path"), base_dir / "wg.pub")
    rp_priv_path = _expand_path(rp_data.get("private_key_path"), base_dir / "rosenpass.key")
    rp_pub_path = _expand_path(rp_data.get("public_key_path"), base_dir / "rosenpass.pub")

    wg_cfg = WireGuardConfig(
        interface=interface,
        address=str(wg_data.get("address") or "").strip() or None,
        listen_port=_coerce_int(wg_data.get("listen_port")),
        mtu=_coerce_int(wg_data.get("mtu")),
        table=str(wg_data.get("table") or "").strip() or None,
        private_key_path=wg_priv_path,
        public_key_path=wg_pub_path,
    )
    cmd_raw = rp_data.get("command")
    cmd_list: list[str] | None
    if isinstance(cmd_raw, list):
        cmd_list = [str(item) for item in cmd_raw if str(item).strip()]
    elif isinstance(cmd_raw, str):
        cmd_list = shlex.split(cmd_raw)
    else:
        cmd_list = None
    rp_cfg = RosenpassConfig(
        private_key_path=rp_priv_path,
        public_key_path=rp_pub_path,
        listen=str(rp_data.get("listen") or "").strip() or None,
        command=cmd_list,
        log_level=_normalize_verbosity(rp_data.get("log_level")),
    )

    peers: list[PeerConfig] = []
    for item in data.get("peers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("node_id") or "peer").strip()
        wg_pub = str(item.get("wg_pubkey") or "").strip()
        if not wg_pub:
            continue
        allowed = _coerce_list(item.get("allowed_ips"))
        peer = PeerConfig(
            name=name,
            wg_pubkey=wg_pub,
            rosenpass_pubkey=str(item.get("rosenpass_pubkey") or item.get("rp_pubkey") or "").strip() or None,
            allowed_ips=allowed,
            endpoint=str(item.get("endpoint") or "").strip() or None,
            persistent_keepalive=_coerce_int(item.get("persistent_keepalive")),
            role=_safe_role(item.get("role")),
        )
        peers.append(peer)

    return RosenpassNodeConfig(
        interface=interface,
        wireguard=wg_cfg,
        rosenpass=rp_cfg,
        peers=peers,
        peers_source=str(peers_source).strip().lower() if peers_source else None,
    )


def ensure_wg_keys(config: RosenpassNodeConfig) -> KeyMaterial | None:
    wg_bin = shutil.which("wg") or "wg"
    priv = _read_text(config.wireguard.private_key_path)
    pub = _read_text(config.wireguard.public_key_path)
    if not priv:
        try:
            priv = subprocess.check_output([wg_bin, "genkey"], text=True).strip()
            _write_private_file(config.wireguard.private_key_path, priv + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("wireguard keygen failed: %s", exc)
            return None
    if not pub:
        try:
            pub = subprocess.check_output(
                [wg_bin, "pubkey"],
                input=(priv + "\n").encode("utf-8"),
            ).decode("utf-8").strip()
            _write_private_file(config.wireguard.public_key_path, pub + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("wireguard pubkey failed: %s", exc)
            return None
    return KeyMaterial(
        private_key=priv,
        public_key=pub,
        private_path=config.wireguard.private_key_path,
        public_path=config.wireguard.public_key_path,
    )


def ensure_rosenpass_keys(config: RosenpassNodeConfig) -> KeyMaterial | None:
    rp_bin = shutil.which("rosenpass") or "rosenpass"
    priv_bytes = _read_bytes(config.rosenpass.private_key_path)
    pub_bytes = _read_bytes(config.rosenpass.public_key_path)
    if priv_bytes and pub_bytes:
        return KeyMaterial(
            private_key=_b64encode(priv_bytes),
            public_key=_b64encode(pub_bytes),
            private_path=config.rosenpass.private_key_path,
            public_path=config.rosenpass.public_key_path,
        )
    if shutil.which("rosenpass") is None:
        LOGGER.warning("rosenpass binary not found; skipping key generation")
        return None
    try:
        config.rosenpass.private_key_path.parent.mkdir(parents=True, exist_ok=True)
        config.rosenpass.public_key_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    candidates = [
        [
            rp_bin,
            "gen-keys",
            "--secret-key",
            str(config.rosenpass.private_key_path),
            "--public-key",
            str(config.rosenpass.public_key_path),
            "--force",
        ],
        [rp_bin, "genkey", str(config.rosenpass.private_key_path), str(config.rosenpass.public_key_path)],
        [rp_bin, "keygen", str(config.rosenpass.private_key_path), str(config.rosenpass.public_key_path)],
        [
            rp_bin,
            "genkey",
            "--private",
            str(config.rosenpass.private_key_path),
            "--public",
            str(config.rosenpass.public_key_path),
        ],
        [
            rp_bin,
            "keygen",
            "--private",
            str(config.rosenpass.private_key_path),
            "--public",
            str(config.rosenpass.public_key_path),
        ],
    ]
    last_error = None
    last_output = None
    for cmd in candidates:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
            if res.returncode == 0:
                last_error = None
                last_output = (res.stderr or res.stdout or "").strip()
                break
            stderr = (res.stderr or "").strip()
            stdout = (res.stdout or "").strip()
            detail = stderr or stdout or f"exit={res.returncode}"
            last_error = detail
        except Exception as exc:
            last_error = str(exc)
            continue
    priv_bytes = _read_bytes(config.rosenpass.private_key_path)
    pub_bytes = _read_bytes(config.rosenpass.public_key_path)
    if not priv_bytes or not pub_bytes:
        if last_error:
            LOGGER.warning("rosenpass key generation failed: %s", last_error)
        elif last_output:
            LOGGER.warning("rosenpass key generation produced no keys: %s", last_output)
        else:
            LOGGER.warning("rosenpass key generation failed; provide keys manually")
        return None
    try:
        os.chmod(config.rosenpass.private_key_path, 0o600)
        os.chmod(config.rosenpass.public_key_path, 0o600)
    except Exception:
        pass
    return KeyMaterial(
        private_key=_b64encode(priv_bytes),
        public_key=_b64encode(pub_bytes),
        private_path=config.rosenpass.private_key_path,
        public_path=config.rosenpass.public_key_path,
    )


def render_wireguard_config(config: RosenpassNodeConfig, wg_private_key: str) -> str:
    lines = ["[Interface]"]
    lines.append(f"PrivateKey = {wg_private_key}")
    if config.wireguard.address:
        lines.append(f"Address = {config.wireguard.address}")
    if config.wireguard.listen_port:
        lines.append(f"ListenPort = {config.wireguard.listen_port}")
    if config.wireguard.mtu:
        lines.append(f"MTU = {config.wireguard.mtu}")
    if config.wireguard.table:
        lines.append(f"Table = {config.wireguard.table}")
    for peer in config.peers:
        lines.append("")
        lines.append("[Peer]")
        lines.append(f"PublicKey = {peer.wg_pubkey}")
        if peer.allowed_ips:
            lines.append(f"AllowedIPs = {', '.join(peer.allowed_ips)}")
        if peer.endpoint:
            lines.append(f"Endpoint = {peer.endpoint}")
        if peer.persistent_keepalive:
            lines.append(f"PersistentKeepalive = {peer.persistent_keepalive}")
    return "\n".join(lines).strip() + "\n"


def render_rosenpass_stub(config: RosenpassNodeConfig, rp_keys: KeyMaterial) -> str:
    lines = [
        "# Autogenerated by k1s (best-effort).",
        "# If Rosenpass fails to start, set rosenpass.command explicitly.",
        f"public_key = \"{rp_keys.public_path}\"",
        f"secret_key = \"{rp_keys.private_path}\"",
    ]
    if config.rosenpass.listen:
        lines.append(f"listen = [\"{config.rosenpass.listen}\"]")
    else:
        lines.append("listen = []")
    if config.rosenpass.log_level:
        lines.append(f"verbosity = \"{config.rosenpass.log_level}\"")
    else:
        lines.append("verbosity = \"Quiet\"")
    for peer in config.peers:
        if not peer.rosenpass_pubkey:
            continue
        lines.append("")
        lines.append("[[peers]]")
        lines.append(f"public_key = \"{peer.rosenpass_pubkey}\"")
        if peer.endpoint:
            lines.append(f"endpoint = \"{peer.endpoint}\"")
        wg_cmd = [
            "wg",
            "set",
            config.interface,
            "peer",
            peer.wg_pubkey,
            "preshared-key",
            "/dev/stdin",
        ]
        rendered_cmd = ", ".join(f"\"{item}\"" for item in wg_cmd)
        lines.append(f"exchange_command = [{rendered_cmd}]")
    return "\n".join(lines).strip() + "\n"


def _materialize_peer_keys(peers: list[PeerConfig], base_dir: Path) -> list[PeerConfig]:
    if not peers:
        return peers
    peers_dir = base_dir / "peers"
    out: list[PeerConfig] = []
    for peer in peers:
        rp_key = peer.rosenpass_pubkey
        if not rp_key:
            out.append(peer)
            continue
        expanded = os.path.expandvars(os.path.expanduser(rp_key))
        candidate = Path(expanded)
        if candidate.is_absolute() and candidate.exists():
            out.append(
                PeerConfig(
                    name=peer.name,
                    wg_pubkey=peer.wg_pubkey,
                    rosenpass_pubkey=str(candidate),
                    allowed_ips=peer.allowed_ips,
                    endpoint=peer.endpoint,
                    persistent_keepalive=peer.persistent_keepalive,
                    role=peer.role,
                )
            )
            continue
        data = _decode_b64(rp_key)
        if data is None:
            try:
                data = bytes.fromhex(rp_key)
            except Exception:
                data = None
        if data is None:
            LOGGER.warning("rosenpass pubkey for %s is not a path or base64; skipping", peer.name)
            out.append(
                PeerConfig(
                    name=peer.name,
                    wg_pubkey=peer.wg_pubkey,
                    rosenpass_pubkey=None,
                    allowed_ips=peer.allowed_ips,
                    endpoint=peer.endpoint,
                    persistent_keepalive=peer.persistent_keepalive,
                    role=peer.role,
                )
            )
            continue
        try:
            peers_dir.mkdir(parents=True, exist_ok=True)
            filename = _sanitize_name(peer.name) + ".rp.pub"
            path = peers_dir / filename
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("failed to write rosenpass pubkey for %s: %s", peer.name, exc)
            out.append(peer)
            continue
        out.append(
            PeerConfig(
                name=peer.name,
                wg_pubkey=peer.wg_pubkey,
                rosenpass_pubkey=str(path),
                allowed_ips=peer.allowed_ips,
                endpoint=peer.endpoint,
                persistent_keepalive=peer.persistent_keepalive,
                role=peer.role,
            )
        )
    return out


def _peer_signature(peers: list[PeerConfig]) -> tuple[tuple]:
    sigs: list[tuple] = []
    for peer in peers:
        rp_path = peer.rosenpass_pubkey or ""
        rp_mtime = None
        if rp_path:
            try:
                rp_mtime = Path(rp_path).stat().st_mtime_ns
            except Exception:
                rp_mtime = None
        sigs.append(
            (
                peer.wg_pubkey,
                rp_path,
                rp_mtime,
                peer.endpoint or "",
                tuple(sorted(peer.allowed_ips or [])),
                peer.role or "",
                peer.persistent_keepalive or 0,
            )
        )
    return tuple(sorted(sigs))


class RosenpassPeerRefresher:
    def __init__(
        self,
        *,
        base_dir: Path,
        config: RosenpassNodeConfig,
        wg_keys: KeyMaterial,
        rp_keys: KeyMaterial | None,
        controller_url: str | None,
        token: str | None,
        node_id: str,
        bootstrap_payload: dict[str, Any] | None,
        refresh_interval: float,
    ) -> None:
        self._base_dir = base_dir
        self._config = config
        self._wg_keys = wg_keys
        self._rp_keys = rp_keys
        self._controller_url = controller_url
        self._token = token
        self._node_id = node_id
        self._bootstrap_payload = bootstrap_payload
        self._refresh_interval = max(5.0, refresh_interval)
        self._stop = threading.Event()
        self._supervisor: RosenpassSupervisor | None = None
        self._peer_sig: tuple[tuple] | None = None

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="rosenpass-peer-refresh")
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._supervisor:
            self._supervisor.stop()

    def _restart_supervisor(self, command: list[str], status_path: Path, env: dict[str, str]) -> None:
        if self._supervisor:
            self._supervisor.stop()
        self._supervisor = RosenpassSupervisor(command, status_path=status_path, env=env)
        self._supervisor.start()

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                if self._bootstrap_payload:
                    payload = dict(self._bootstrap_payload)
                    payload.setdefault("node_id", self._node_id)
                    payload["wg_pubkey"] = self._wg_keys.public_key
                    if self._rp_keys:
                        payload["rp_pubkey"] = self._rp_keys.public_key
                    _bootstrap_heartbeat(self._controller_url, payload, token=self._token)
                peers = fetch_peers_from_controller(self._controller_url, self._node_id, token=self._token)
                LOGGER.debug("rosenpass peer refresh fetched %s peers", len(peers or []))
                if peers:
                    peers = _materialize_peer_keys(peers, self._base_dir)
                sig = _peer_signature(peers)
                if sig != self._peer_sig:
                    self._peer_sig = sig
                    LOGGER.debug("rosenpass peer refresh applying wireguard (%s peers)", len(peers or []))
                    config = RosenpassNodeConfig(
                        interface=self._config.interface,
                        wireguard=self._config.wireguard,
                        rosenpass=self._config.rosenpass,
                        peers=peers,
                        peers_source=self._config.peers_source,
                    )
                    wg_config = render_wireguard_config(config, self._wg_keys.private_key)
                    _maybe_dump_wg_config(self._base_dir, wg_config)
                    try:
                        from ae.node.net_helper import apply_wireguard

                        apply_wireguard(wg_config, iface=config.interface)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("wireguard apply failed: %s", exc)
                    has_rp_peers = _peer_has_rosenpass_keys(peers)
                    if self._rp_keys and has_rp_peers:
                        stub_path = self._base_dir / "rosenpass.conf"
                        try:
                            stub_path.write_text(render_rosenpass_stub(config, self._rp_keys), encoding="utf-8")
                        except Exception:
                            pass
                        command = build_command(config, config_path=stub_path)
                        if command:
                            env = {}
                            if config.rosenpass.log_level:
                                env["ROSENPASS_LOG"] = config.rosenpass.log_level
                            self._restart_supervisor(
                                command, self._base_dir / "rosenpass-status.json", env
                            )
                    else:
                        _write_status_file(
                            self._base_dir / "rosenpass-status.json",
                            {
                                "state": "waiting-for-peers",
                                "reason": "no_rosenpass_peers" if peers else "no_peers",
                                "peer_count": len(peers or []),
                                "last_update": time.time(),
                            },
                        )
                        if self._supervisor:
                            self._supervisor.stop()
                            self._supervisor = None
                backoff = 5.0
                time.sleep(self._refresh_interval)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("rosenpass peer refresh failed: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


def _format_command(cmd: list[str], **kwargs: str) -> list[str]:
    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    safe = _Safe(kwargs)
    out = []
    for part in cmd:
        try:
            out.append(part.format_map(safe))
        except Exception:
            out.append(part)
    return out


def build_command(
    config: RosenpassNodeConfig,
    *,
    config_path: Path,
) -> list[str] | None:
    if config.rosenpass.command:
        return _format_command(
            config.rosenpass.command,
            config=str(config_path),
            interface=config.interface,
            rp_private=str(config.rosenpass.private_key_path),
            rp_public=str(config.rosenpass.public_key_path),
        )
    if shutil.which("rosenpass") is None:
        return None
    return ["rosenpass", "exchange-config", str(config_path)]


def fetch_peers_from_controller(
    controller_url: str | None,
    node_id: str,
    token: str | None = None,
) -> list[PeerConfig]:
    if not controller_url:
        LOGGER.warning("controller URL not set; cannot fetch overlay peers")
        return []
    import requests

    url = controller_url.rstrip("/") + f"/v1/nodes/{node_id}/overlay"
    headers = {}
    if token:
        headers["X-Agent-Token"] = token
    try:
        resp = requests.get(url, headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("overlay peer fetch failed: %s", exc)
        return []
    if resp.status_code != 200:
        LOGGER.warning("overlay peer fetch failed: %s", resp.text)
        return []
    try:
        payload = resp.json()
    except Exception:
        return []
    peers = []
    for item in payload.get("peers") or []:
        if not isinstance(item, dict):
            continue
        wg_pub = str(item.get("wg_pubkey") or "").strip()
        if not wg_pub:
            continue
        peers.append(
            PeerConfig(
                name=str(item.get("name") or item.get("node_id") or "peer").strip(),
                wg_pubkey=wg_pub,
                rosenpass_pubkey=str(item.get("rosenpass_pubkey") or "").strip() or None,
                allowed_ips=_coerce_list(item.get("allowed_ips")),
                endpoint=str(item.get("endpoint") or "").strip() or None,
                persistent_keepalive=_coerce_int(item.get("persistent_keepalive")),
                role=_safe_role(item.get("role")),
            )
        )
    return peers


def _bootstrap_heartbeat(
    controller_url: str | None,
    payload: dict[str, Any],
    token: str | None = None,
) -> bool:
    if not controller_url:
        return False
    import requests

    url = controller_url.rstrip("/") + "/v1/heartbeat"
    headers = {}
    if token:
        headers["X-Agent-Token"] = token
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("overlay bootstrap heartbeat failed: %s", exc)
        return False
    if resp.status_code != 200:
        LOGGER.warning("overlay bootstrap heartbeat failed: %s", resp.text)
        return False
    return True


class RosenpassSupervisor:
    def __init__(
        self,
        command: list[str],
        *,
        status_path: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._status_path = status_path
        self._env = env or {}
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True, name="rosenpass-supervisor")
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _write_status(self, payload: dict[str, Any]) -> None:
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            start_ts = time.time()
            self._write_status(
                {
                    "state": "starting",
                    "command": self._command,
                    "last_start": start_ts,
                }
            )
            try:
                proc = subprocess.Popen(  # noqa: S603
                    self._command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, **self._env},
                    text=True,
                )
                with self._lock:
                    self._proc = proc
            except Exception as exc:  # noqa: BLE001
                self._write_status(
                    {
                        "state": "error",
                        "command": self._command,
                        "last_start": start_ts,
                        "error": str(exc),
                    }
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._write_status(
                {
                    "state": "running",
                    "pid": proc.pid,
                    "command": self._command,
                    "last_start": start_ts,
                }
            )
            stdout, stderr = proc.communicate()
            with self._lock:
                self._proc = None
            exit_ts = time.time()
            payload = {
                "state": "exited",
                "pid": proc.pid,
                "command": self._command,
                "last_start": start_ts,
                "last_exit": exit_ts,
                "exit_code": proc.returncode,
            }
            if stderr:
                payload["stderr_tail"] = stderr.strip()[-2000:]
            if stdout:
                payload["stdout_tail"] = stdout.strip()[-2000:]
            self._write_status(payload)
            if self._stop.is_set():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def prepare_rosenpass(
    *,
    config_path: Path | None,
    base_dir: Path,
    controller_url: str | None,
    token: str | None,
    node_id: str,
    pod_cidr: str | None = None,
    peers_from_controller: bool = False,
    bootstrap_payload: dict[str, Any] | None = None,
    peer_refresh_sec: float | None = None,
) -> RosenpassRuntime | None:
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    if config_path and config_path.exists():
        config = load_config(config_path, base_dir=base_dir)
    else:
        if config_path is not None and not config_path.exists():
            LOGGER.warning("rosenpass config not found: %s", config_path)
        config = load_env_config(base_dir, pod_cidr=pod_cidr)
    wg_keys = ensure_wg_keys(config)
    rp_keys = ensure_rosenpass_keys(config)
    if not wg_keys:
        return None
    if peers_from_controller or config.peers_source == "controller":
        if bootstrap_payload:
            payload = dict(bootstrap_payload)
            payload.setdefault("node_id", node_id)
            payload["wg_pubkey"] = wg_keys.public_key
            if rp_keys:
                payload["rp_pubkey"] = rp_keys.public_key
            _bootstrap_heartbeat(controller_url, payload, token=token)
        peers = fetch_peers_from_controller(controller_url, node_id, token=token)
        if peers:
            config = RosenpassNodeConfig(
                interface=config.interface,
                wireguard=config.wireguard,
                rosenpass=config.rosenpass,
                peers=peers,
                peers_source=config.peers_source,
            )
    if config.peers:
        config = RosenpassNodeConfig(
            interface=config.interface,
            wireguard=config.wireguard,
            rosenpass=config.rosenpass,
            peers=_materialize_peer_keys(config.peers, base_dir),
            peers_source=config.peers_source,
        )
    refresh_sec = peer_refresh_sec if peer_refresh_sec is not None else 0.0
    wg_config = render_wireguard_config(config, wg_keys.private_key)
    _maybe_dump_wg_config(base_dir, wg_config)
    status_path = base_dir / "rosenpass-status.json"
    if rp_keys and not _peer_has_rosenpass_keys(config.peers):
        _write_status_file(
            status_path,
            {
                "state": "waiting-for-peers",
                "reason": "no_rosenpass_peers",
                "peer_count": len(config.peers or []),
                "last_update": time.time(),
            },
        )
        if peers_from_controller and refresh_sec > 0:
            refresher = RosenpassPeerRefresher(
                base_dir=base_dir,
                config=config,
                wg_keys=wg_keys,
                rp_keys=rp_keys,
                controller_url=controller_url,
                token=token,
                node_id=node_id,
                bootstrap_payload=bootstrap_payload,
                refresh_interval=refresh_sec,
            )
            refresher.start()
        return RosenpassRuntime(
            interface=config.interface,
            wg_config=wg_config,
            wg_public_key=wg_keys.public_key if wg_keys else None,
            rp_public_key=rp_keys.public_key if rp_keys else None,
            status_path=status_path,
        )
    if rp_keys:
        stub_path = base_dir / "rosenpass.conf"
        try:
            stub_path.write_text(render_rosenpass_stub(config, rp_keys), encoding="utf-8")
        except Exception:
            pass
        command = build_command(config, config_path=stub_path)
        if command:
            env = {}
            if config.rosenpass.log_level:
                env["ROSENPASS_LOG"] = config.rosenpass.log_level
            sup = RosenpassSupervisor(command, status_path=status_path, env=env)
            sup.start()
        else:
            LOGGER.warning("rosenpass command not configured; skipping run")
        if peers_from_controller and refresh_sec > 0:
            refresher = RosenpassPeerRefresher(
                base_dir=base_dir,
                config=config,
                wg_keys=wg_keys,
                rp_keys=rp_keys,
                controller_url=controller_url,
                token=token,
                node_id=node_id,
                bootstrap_payload=bootstrap_payload,
                refresh_interval=refresh_sec,
            )
            refresher.start()
    return RosenpassRuntime(
        interface=config.interface,
        wg_config=wg_config,
        wg_public_key=wg_keys.public_key if wg_keys else None,
        rp_public_key=rp_keys.public_key if rp_keys else None,
        status_path=status_path,
    )


# ruff: noqa: E501,UP006,UP007,UP017,S110,S112,SIM105
