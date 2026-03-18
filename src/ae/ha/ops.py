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
from urllib.parse import urlparse, urlunparse

HA_CORE_REQUIRED_ENV: tuple[str, ...] = (
    "AE_CONTROLLER_ID",
    "AE_CONTROLLER_ADVERTISE_ADDR",
    "AE_ETCD_ENDPOINTS",
    "AE_ETCD_PREFIX",
    "AE_NATS_URL",
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROM_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$")
_ETCD_ENV_LINE = re.compile(r'^(?P<key>ETCD_[A-Z0-9_]+)=(?:"(?P<quoted>.*)"|(?P<raw>.+))$')
_ETCD_MEMBER_ADD = re.compile(
    r"Member\s+(?P<member_id>[0-9a-fA-F]+)\s+added(?:\s+to\s+cluster\s+(?P<cluster_id>[0-9a-fA-F]+))?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EtcdLeaderRecord:
    controller_id: str
    controller_epoch: int
    advertise_addr: str | None
    lease_id: int
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EtcdMemberAddResult:
    member_id: str | None
    cluster_id: str | None
    member_name: str
    initial_cluster: str
    initial_cluster_state: str
    initial_advertise_peer_urls: str | None
    raw_env: dict[str, str]


@dataclass(frozen=True, slots=True)
class EtcdRestoreMemberSpec:
    name: str
    peer_url: str
    client_url: str
    data_dir: str


@dataclass(frozen=True, slots=True)
class EtcdRestoreMemberPlan:
    name: str
    peer_url: str
    client_url: str
    data_dir: str
    restore_command: list[str]
    start_command: list[str]


@dataclass(frozen=True, slots=True)
class EtcdQuorumRestorePlan:
    snapshot_path: str
    initial_cluster: str
    initial_cluster_token: str
    members: list[EtcdRestoreMemberPlan]


@dataclass(frozen=True, slots=True)
class BuildInfoRecord:
    component: str
    version: str
    sha: str
    date: str


@dataclass(frozen=True, slots=True)
class HaCoreNodeTarget:
    name: str
    controller_url: str
    apishim_url: str


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


def collect_prometheus_metric_values(
    text: str,
    metric_name: str,
) -> list[tuple[dict[str, str], float]]:
    values: list[tuple[dict[str, str], float]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE.match(line)
        if not match or match.group("name") != metric_name:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        values.append((_parse_prometheus_labels(match.group("labels") or ""), value))
    return values


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


def _build_local_etcdctl_prefix(
    *,
    endpoints: list[str] | None = None,
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
    return cmd, {"ETCDCTL_API": "3"}


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
    cmd, env = _build_local_etcdctl_prefix(
        endpoints=endpoints,
        binary=binary,
        ca_cert=ca_cert,
        cert=cert,
        key=key,
        user=user,
        password=password,
    )
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


def build_local_etcdctl_recovery_command(
    action: str,
    *,
    endpoints: list[str] | None = None,
    member_id: str | None = None,
    member_name: str | None = None,
    peer_urls: str | None = None,
    output: str = "table",
    cluster: bool = False,
    binary: str = "etcdctl",
    ca_cert: str | None = None,
    cert: str | None = None,
    key: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    cmd, env = _build_local_etcdctl_prefix(
        endpoints=endpoints,
        binary=binary,
        ca_cert=ca_cert,
        cert=cert,
        key=key,
        user=user,
        password=password,
    )
    normalized_output = str(output or "table").strip().lower() or "table"
    if action == "endpoint-status":
        cmd += ["endpoint", "status"]
        if cluster:
            cmd.append("--cluster")
        cmd.append(f"-w={normalized_output}")
    elif action == "member-list":
        cmd += ["member", "list", f"-w={normalized_output}"]
    elif action == "member-remove":
        if not member_id:
            raise ValueError("member_id required for member-remove")
        cmd += ["member", "remove", str(member_id)]
    elif action == "member-add":
        if not member_name:
            raise ValueError("member_name required for member-add")
        if not peer_urls:
            raise ValueError("peer_urls required for member-add")
        cmd += ["member", "add", str(member_name), f"--peer-urls={peer_urls}", "--learner"]
    elif action == "member-promote":
        if not member_id:
            raise ValueError("member_id required for member-promote")
        cmd += ["member", "promote", str(member_id)]
    else:
        raise ValueError(f"unsupported etcdctl recovery action: {action}")
    return cmd, env


def detect_container_cli() -> str | None:
    explicit = str(os.getenv("AE_CONTAINER_CLI") or "").strip()
    if explicit:
        return explicit
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def resolve_etcdctl_runner(mode: str, etcdctl_bin: str) -> str:
    if mode == "local":
        if shutil.which(etcdctl_bin) is None:
            raise RuntimeError(f"local etcdctl not found: {etcdctl_bin}")
        return "local"
    if mode == "container":
        detect_container_cli_or_die()
        return "container"
    if shutil.which(etcdctl_bin) is not None:
        return "local"
    detect_container_cli_or_die()
    return "container"


def detect_container_cli_or_die() -> str:
    cli = detect_container_cli()
    if not cli:
        raise RuntimeError("no container CLI found; install etcdctl or set AE_CONTAINER_CLI")
    return cli


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


def required_parent_mounts(paths: list[str | Path | None]) -> list[Path]:
    mounts: list[Path] = []
    seen: set[Path] = set()
    for item in paths:
        if item is None:
            continue
        raw = str(item).strip()
        if not raw:
            continue
        parent = Path(raw).expanduser().resolve().parent
        if parent in seen:
            continue
        seen.add(parent)
        parent.mkdir(parents=True, exist_ok=True)
        mounts.append(parent)
    return mounts


def parse_etcd_member_add_output(
    text: str,
    *,
    expected_name: str | None = None,
    expected_peer_urls: str | None = None,
) -> EtcdMemberAddResult:
    member_id: str | None = None
    cluster_id: str | None = None
    env_map: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        member_match = _ETCD_MEMBER_ADD.search(line)
        if member_match:
            member_id = member_match.group("member_id")
            cluster_id = member_match.group("cluster_id")
            continue
        env_match = _ETCD_ENV_LINE.match(line)
        if env_match:
            env_map[env_match.group("key")] = env_match.group("quoted") or env_match.group("raw") or ""
    member_name = str(env_map.get("ETCD_NAME") or expected_name or "").strip()
    initial_cluster = str(env_map.get("ETCD_INITIAL_CLUSTER") or "").strip()
    initial_cluster_state = str(env_map.get("ETCD_INITIAL_CLUSTER_STATE") or "").strip()
    initial_peer_urls = (
        str(env_map.get("ETCD_INITIAL_ADVERTISE_PEER_URLS") or expected_peer_urls or "").strip() or None
    )
    if not member_name or not initial_cluster or not initial_cluster_state:
        raise ValueError(f"unable to parse etcd member add output: {text!r}")
    return EtcdMemberAddResult(
        member_id=member_id,
        cluster_id=cluster_id,
        member_name=member_name,
        initial_cluster=initial_cluster,
        initial_cluster_state=initial_cluster_state,
        initial_advertise_peer_urls=initial_peer_urls,
        raw_env=env_map,
    )


def derive_client_url(peer_url: str) -> str:
    parsed = urlparse(str(peer_url or "").strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"invalid peer url: {peer_url!r}")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth = f"{auth}:{parsed.password}"
        netloc = f"{auth}@{netloc}"
    netloc = f"{netloc}:2379"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def build_quorum_restore_plan(
    *,
    snapshot_path: Path,
    cluster_token: str,
    members: list[EtcdRestoreMemberSpec],
    binary: str = "etcdctl",
) -> EtcdQuorumRestorePlan:
    if len(members) != 3:
        raise ValueError("exactly three members are required for quorum restore planning")
    initial_cluster = ",".join(f"{member.name}={member.peer_url}" for member in members)
    plans: list[EtcdRestoreMemberPlan] = []
    for member in members:
        restore_cmd, _env = build_local_etcdctl_command(
            "restore",
            snapshot_path=snapshot_path,
            data_dir=Path(member.data_dir),
            name=member.name,
            initial_cluster=initial_cluster,
            initial_advertise_peer_urls=member.peer_url,
            initial_cluster_token=cluster_token,
            binary=binary,
        )
        start_cmd = [
            "etcd",
            f"--name={member.name}",
            f"--data-dir={member.data_dir}",
            f"--listen-peer-urls={member.peer_url}",
            f"--initial-advertise-peer-urls={member.peer_url}",
            f"--listen-client-urls={member.client_url}",
            f"--advertise-client-urls={member.client_url}",
            f"--initial-cluster={initial_cluster}",
            "--initial-cluster-state=new",
            f"--initial-cluster-token={cluster_token}",
        ]
        plans.append(
            EtcdRestoreMemberPlan(
                name=member.name,
                peer_url=member.peer_url,
                client_url=member.client_url,
                data_dir=member.data_dir,
                restore_command=restore_cmd,
                start_command=start_cmd,
            )
        )
    return EtcdQuorumRestorePlan(
        snapshot_path=str(snapshot_path),
        initial_cluster=initial_cluster,
        initial_cluster_token=cluster_token,
        members=plans,
    )


def format_quorum_restore_plan(plan: EtcdQuorumRestorePlan) -> str:
    lines = [
        f"Quorum restore plan from snapshot: {plan.snapshot_path}",
        f"Initial cluster: {plan.initial_cluster}",
        f"Initial cluster token: {plan.initial_cluster_token}",
        "",
        "Assumption: each member listens and advertises on the same peer/client URLs shown below.",
    ]
    for member in plan.members:
        lines.extend(
            [
                "",
                f"[{member.name}]",
                f"peer_url={member.peer_url}",
                f"client_url={member.client_url}",
                f"data_dir={member.data_dir}",
                "restore:",
                f"  {' '.join(member.restore_command)}",
                "start:",
                f"  {' '.join(member.start_command)}",
            ]
        )
    return "\n".join(lines)


def parse_ha_core_node_target(raw: str) -> HaCoreNodeTarget:
    text = str(raw or "").strip()
    if "=" not in text:
        raise ValueError(f"invalid node value {raw!r}; expected NAME=CONTROLLER_URL,APISHIM_URL")
    name, urls_raw = text.split("=", 1)
    node_name = name.strip()
    urls = [item.strip() for item in urls_raw.split(",") if item.strip()]
    if len(urls) != 2:
        raise ValueError(f"invalid node value {raw!r}; expected NAME=CONTROLLER_URL,APISHIM_URL")
    if not node_name:
        raise ValueError(f"invalid node value {raw!r}; missing node name")
    return HaCoreNodeTarget(
        name=node_name,
        controller_url=urls[0].rstrip("/"),
        apishim_url=urls[1].rstrip("/"),
    )


def fetch_http_text(url: str, *, timeout_s: float = 3.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8")


def fetch_build_info(base_url: str, *, timeout_s: float = 3.0) -> BuildInfoRecord:
    payload = _http_json(f"{str(base_url or '').rstrip('/')}/__ae/version", timeout_s=timeout_s)
    return BuildInfoRecord(
        component=str(payload.get("component") or ""),
        version=str(payload.get("version") or ""),
        sha=str(payload.get("sha") or ""),
        date=str(payload.get("date") or ""),
    )


def subprocess_run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        check=True,
        text=True,
        capture_output=capture_output,
        env={**os.environ, **(env or {})},
    )


__all__ = [
    "BuildInfoRecord",
    "EtcdMemberAddResult",
    "EtcdQuorumRestorePlan",
    "EtcdLeaderRecord",
    "EtcdRestoreMemberPlan",
    "EtcdRestoreMemberSpec",
    "HA_CORE_REQUIRED_ENV",
    "HaCoreNodeTarget",
    "build_container_etcdctl_command",
    "build_local_etcdctl_command",
    "build_local_etcdctl_recovery_command",
    "build_quorum_restore_plan",
    "collect_prometheus_metric_values",
    "detect_container_cli",
    "detect_container_cli_or_die",
    "derive_client_url",
    "etcd_endpoint_healthy",
    "fetch_build_info",
    "fetch_http_text",
    "format_quorum_restore_plan",
    "ha_core_missing_env",
    "healthy_etcd_endpoints",
    "is_loopback_host",
    "leader_key",
    "parse_etcd_leader_response",
    "parse_etcd_member_add_output",
    "parse_ha_core_node_target",
    "parse_nats_url",
    "parse_prometheus_metric_value",
    "read_etcd_leader",
    "required_parent_mounts",
    "resolve_etcdctl_runner",
    "split_csv",
    "subprocess_run",
    "tcp_connectable",
]
