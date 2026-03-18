from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HA_CORE_REQUIRED_ENV: tuple[str, ...] = (
    "AE_CONTROLLER_ID",
    "AE_CONTROLLER_ADVERTISE_ADDR",
    "AE_ETCD_ENDPOINTS",
    "AE_ETCD_PREFIX",
    "AE_NATS_URL",
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROM_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$")


@dataclass(frozen=True, slots=True)
class EtcdLeaderRecord:
    controller_id: str
    controller_epoch: int
    advertise_addr: str | None
    lease_id: int
    raw: dict[str, Any]


def split_csv(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def ha_core_missing_env(env: dict[str, str] | None = None) -> list[str]:
    env_map = env or os.environ
    return [key for key in HA_CORE_REQUIRED_ENV if not str(env_map.get(key) or "").strip()]


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    return str(host).strip().lower() in _LOOPBACK_HOSTS


def parse_nats_url(url: str) -> tuple[str, int]:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("missing nats url")
    if "://" not in raw:
        raw = f"nats://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise ValueError("missing host")
    return parsed.hostname, int(parsed.port or 4222)


def tcp_connectable(host: str, port: int, timeout_s: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True, "ok"
    except OSError as exc:
        return False, str(exc)


def _http_json(url: str, *, timeout_s: float = 3.0, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data: bytes | None = None
    method = "GET"
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw or "{}")


def etcd_endpoint_healthy(endpoint: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    target = str(endpoint or "").strip().rstrip("/")
    if not target:
        return False, "missing endpoint"
    try:
        payload = _http_json(f"{target}/health", timeout_s=timeout_s)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)
    health = str(payload.get("health") or "").strip().lower()
    if health in {"true", "1", "ok"}:
        return True, "ok"
    return False, f"unexpected health payload: {payload}"


def healthy_etcd_endpoints(endpoints: list[str], timeout_s: float = 3.0) -> list[str]:
    healthy: list[str] = []
    for endpoint in endpoints:
        ok, _ = etcd_endpoint_healthy(endpoint, timeout_s=timeout_s)
        if ok:
            healthy.append(endpoint)
    return healthy


def leader_key(etcd_prefix: str) -> str:
    prefix = str(etcd_prefix or "").strip("/")
    return "/".join(part for part in (prefix, "controlplane", "leader") if part)


def parse_etcd_leader_response(payload: dict[str, Any]) -> EtcdLeaderRecord | None:
    kvs = payload.get("kvs") or []
    if not kvs:
        return None
    kv = kvs[0]
    raw_value = base64.b64decode(str(kv.get("value") or "").encode("ascii")).decode("utf-8")
    record = json.loads(raw_value or "{}")
    return EtcdLeaderRecord(
        controller_id=str(record.get("controller_id") or ""),
        controller_epoch=int(kv.get("mod_revision") or 0),
        advertise_addr=(str(record.get("advertise_addr") or "").strip() or None),
        lease_id=int(record.get("lease_id") or 0),
        raw=record,
    )


def read_etcd_leader(
    endpoints: list[str],
    etcd_prefix: str,
    *,
    timeout_s: float = 3.0,
) -> EtcdLeaderRecord | None:
    if not endpoints:
        raise RuntimeError("etcd endpoints required")
    key = leader_key(etcd_prefix)
    payload = {"key": base64.b64encode(key.encode("utf-8")).decode("ascii"), "limit": 1}
    last_exc: Exception | None = None
    for endpoint in endpoints:
        target = endpoint.rstrip("/")
        try:
            response = _http_json(f"{target}/v3/kv/range", timeout_s=timeout_s, payload=payload)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        return parse_etcd_leader_response(response)
    if last_exc is not None:
        raise RuntimeError(f"failed to read leader key: {last_exc}") from last_exc
    return None


def parse_prometheus_metric_value(
    text: str,
    metric_name: str,
    *,
    labels: dict[str, str] | None = None,
) -> float | None:
    wanted = labels or {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE.match(line)
        if not match:
            continue
        if match.group("name") != metric_name:
            continue
        line_labels = _parse_prometheus_labels(match.group("labels") or "")
        if any(line_labels.get(key) != value for key, value in wanted.items()):
            continue
        try:
            return float(match.group("value"))
        except ValueError:
            continue
    return None


def _parse_prometheus_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not raw:
        return labels
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        labels[key.strip()] = value.strip().strip('"')
    return labels


def build_local_etcdctl_command(
    action: str,
    *,
    endpoints: list[str] | None = None,
    snapshot_path: Path | None = None,
    data_dir: Path | None = None,
    name: str | None = None,
    initial_cluster: str | None = None,
    initial_advertise_peer_urls: str | None = None,
    initial_cluster_token: str | None = None,
    binary: str = "etcdctl",
    ca_cert: str | None = None,
    cert: str | None = None,
    key: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    cmd = [binary]
    if endpoints:
        cmd.append(f"--endpoints={','.join(endpoints)}")
    if ca_cert:
        cmd.append(f"--cacert={ca_cert}")
    if cert:
        cmd.append(f"--cert={cert}")
    if key:
        cmd.append(f"--key={key}")
    if user and password:
        cmd.append(f"--user={user}:{password}")
    env = {"ETCDCTL_API": "3"}
    if action == "save":
        if snapshot_path is None:
            raise ValueError("snapshot_path required for save")
        cmd += ["snapshot", "save", str(snapshot_path)]
    elif action == "status":
        if snapshot_path is None:
            raise ValueError("snapshot_path required for status")
        cmd += ["snapshot", "status", str(snapshot_path), "-w", "table"]
    elif action == "restore":
        if snapshot_path is None:
            raise ValueError("snapshot_path required for restore")
        if data_dir is None:
            raise ValueError("data_dir required for restore")
        cmd += ["snapshot", "restore", str(snapshot_path), f"--data-dir={data_dir}"]
        if name:
            cmd.append(f"--name={name}")
        if initial_cluster:
            cmd.append(f"--initial-cluster={initial_cluster}")
        if initial_advertise_peer_urls:
            cmd.append(f"--initial-advertise-peer-urls={initial_advertise_peer_urls}")
        if initial_cluster_token:
            cmd.append(f"--initial-cluster-token={initial_cluster_token}")
    else:
        raise ValueError(f"unsupported etcdctl action: {action}")
    return cmd, env


def detect_container_cli() -> str | None:
    explicit = str(os.getenv("AE_CONTAINER_CLI") or "").strip()
    if explicit:
        return explicit
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def build_container_etcdctl_command(
    container_cli: str,
    image: str,
    inner_cmd: list[str],
    *,
    mounts: list[Path],
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    cmd = [container_cli, "run", "--rm"]
    seen: set[Path] = set()
    for mount in mounts:
        path = mount.resolve()
        if path in seen:
            continue
        seen.add(path)
        cmd += ["-v", f"{path}:{path}"]
    for key, value in sorted((extra_env or {}).items()):
        cmd += ["-e", f"{key}={value}"]
    cmd.append(image)
    cmd.extend(inner_cmd)
    return cmd


def subprocess_run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        check=True,
        text=True,
        capture_output=False,
        env={**os.environ, **(env or {})},
    )


__all__ = [
    "EtcdLeaderRecord",
    "HA_CORE_REQUIRED_ENV",
    "build_container_etcdctl_command",
    "build_local_etcdctl_command",
    "detect_container_cli",
    "etcd_endpoint_healthy",
    "ha_core_missing_env",
    "healthy_etcd_endpoints",
    "is_loopback_host",
    "leader_key",
    "parse_etcd_leader_response",
    "parse_nats_url",
    "parse_prometheus_metric_value",
    "read_etcd_leader",
    "split_csv",
    "subprocess_run",
    "tcp_connectable",
]
