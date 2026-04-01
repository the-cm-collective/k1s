#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E501, S603
import argparse
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ae import build_info as ae_build_info  # noqa: E402

VARIANT_SCRIPT = ROOT / "scripts" / "lab" / "vm" / "lib" / "variant.py"
DEFAULT_LANES = ["single_non_gpu", "single_gpu", "multi_non_gpu", "multi_gpu"]
SUPPORTED_LANES = [*DEFAULT_LANES, "ha_control_plane"]
DEFAULT_PHASE_TIMEOUTS = {
    "provision": 1800,
    "seed_cache": 1200,
    "ha_shared_infra": 900,
    "bootstrap": 1800,
    "service_ready": 900,
    "fabric_validate": 600,
    "functional_basic": 300,
    "ha_acceptance": 900,
}
DEFAULT_RETRY_POLICY = {
    "initial_backoff_s": 2.0,
    "max_backoff_s": 15.0,
    "jitter_s": 1.0,
}
EP = "unix:///run/containerd/containerd.sock"
CRI_SEED_MANIFEST = ROOT / "lab" / "variants" / "cri_seed_images.lock.json"
CRI_SEED_BUNDLE_NAME = "cri-seed-images.oci.tar"
GLOBAL_PHASE_NOISE_MARKERS = (
    "Permanently added ",
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
    "Offending ",
    "This host key is known by the following other names/addresses",
)


@dataclass
class PhaseResult:
    phase: str
    status: str
    started_at: str
    ended_at: str
    duration_s: float
    attempts: int
    detail: str


class SmokeError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def default_run_id() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_unhandled_failure_summary(args: argparse.Namespace, run_id: str, exc: Exception) -> None:
    output_root = (
        Path(args.output_root).expanduser().resolve() if args.output_root else ROOT / "runs"
    )
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": run_id,
        "variant": str(args.variant),
        "status": "failed",
        "reason": "unexpected_exception",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "run_ended_at": iso(utc_now()),
    }
    write_json(run_dir / "summary.json", summary)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    (run_dir / "crash.log").write_text(trace, encoding="utf-8")


def parse_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_label_csv(raw: str | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in parse_csv(str(raw or "")):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            labels[key] = value
    return labels


def _url_host(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"http://{value}")
    return str(parsed.hostname or "").strip()


def parse_phase_overrides(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        for pair in parse_csv(item):
            if "=" not in pair:
                raise SmokeError(f"invalid --phase-timeout value: {pair}")
            key, value = pair.split("=", 1)
            key = key.strip()
            if key not in DEFAULT_PHASE_TIMEOUTS:
                raise SmokeError(f"unknown phase timeout key: {key}")
            try:
                seconds = int(value.strip())
            except ValueError as exc:
                raise SmokeError(f"invalid timeout for {key}: {value}") from exc
            if seconds <= 0:
                raise SmokeError(f"phase timeout must be > 0 for {key}")
            out[key] = seconds
    return out


def parse_retry_policy(raw: str) -> dict[str, float]:
    if not raw:
        return dict(DEFAULT_RETRY_POLICY)
    value = raw.strip().lower()
    if value == "bounded":
        return dict(DEFAULT_RETRY_POLICY)
    if value == "short":
        return {
            "initial_backoff_s": 1.0,
            "max_backoff_s": 4.0,
            "jitter_s": 0.5,
        }
    if value == "none":
        return {
            "initial_backoff_s": 0.0,
            "max_backoff_s": 0.0,
            "jitter_s": 0.0,
        }

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise SmokeError(f"retry policy not found: {raw}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SmokeError("retry policy json must be an object")
    out = dict(DEFAULT_RETRY_POLICY)
    for key in out:
        if key in payload:
            out[key] = float(payload[key])
    for key in ("initial_backoff_s", "max_backoff_s", "jitter_s"):
        if out[key] < 0:
            raise SmokeError(f"retry policy {key} must be >= 0")
    return out


def run_cmd(
    cmd: list[str],
    *,
    timeout: int | None = None,
    check: bool = False,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=check,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise SmokeError(f"command timed out: {' '.join(cmd)}") from exc


def tail_lines(raw: str | None, limit: int = 10) -> list[str]:
    if not raw:
        return []
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    return lines[-limit:]


def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    return any(marker in stripped for marker in GLOBAL_PHASE_NOISE_MARKERS)


def select_failure_detail(stderr: str | None, stdout: str | None) -> str:
    for raw in (stderr, stdout):
        for line in reversed(tail_lines(raw, limit=50)):
            if not is_noise_line(line):
                return line
    for raw in (stderr, stdout):
        lines = tail_lines(raw, limit=50)
        if lines:
            return lines[-1]
    return "command failed"


def load_variant(variant_path: Path) -> dict[str, Any]:
    res = run_cmd(
        [
            sys.executable,
            str(VARIANT_SCRIPT),
            "--variant",
            str(variant_path),
            "--print-json",
        ],
        check=False,
    )
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise SmokeError(stderr or "variant parsing failed")
    return json.loads(res.stdout)


def ssh_base(ip: str) -> list[str]:
    key_path = Path(
        os.environ.get("SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_rsa"))
    ).expanduser()
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-i",
        str(key_path),
        f"ae@{ip}",
    ]


def require_vm_host_prereqs(*, skip_up: bool, auto_down: bool) -> None:
    if not skip_up or auto_down:
        key_path = Path(
            os.environ.get("SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_rsa"))
        ).expanduser()
        if not key_path.is_file():
            raise SmokeError(f"ssh key not found at {key_path}; set SSH_KEY_PATH")
        pubkey_path = Path(f"{key_path}.pub")
        if not pubkey_path.is_file():
            raise SmokeError(f"ssh public key not found at {pubkey_path}")
        res = run_cmd(["sudo", "-n", "true"], check=False)
        if res.returncode != 0:
            raise SmokeError("local sudo credentials are required; run 'sudo -v' and retry")


def run_remote(ip: str, command: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    remote_cmd = f"bash -lc {shlex.quote(command)}"
    return run_cmd([*ssh_base(ip), remote_cmd], timeout=timeout, check=False)


def with_retry(
    check_fn,
    *,
    timeout_s: int,
    retry_policy: dict[str, float],
) -> tuple[bool, int, str, Any]:
    deadline = time.monotonic() + float(timeout_s)
    attempts = 0
    backoff = float(retry_policy["initial_backoff_s"])
    max_backoff = float(retry_policy["max_backoff_s"])
    jitter = float(retry_policy["jitter_s"])
    last_detail = ""
    last_payload: Any = None

    while True:
        attempts += 1
        try:
            ok, detail, payload = check_fn()
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            ok = False
            detail = f"check_exception:{type(exc).__name__}"
            payload = {"error": str(exc)}
        if ok:
            return True, attempts, detail, payload
        last_detail = detail
        last_payload = payload

        now = time.monotonic()
        if now >= deadline:
            return False, attempts, last_detail, last_payload

        if max_backoff <= 0:
            continue

        sleep_s = min(max_backoff, max(backoff, 0.0))
        if jitter > 0:
            sleep_s += random.uniform(0, jitter)  # noqa: S311
        sleep_s = min(sleep_s, max(0.0, deadline - now))
        if sleep_s > 0:
            time.sleep(sleep_s)
        if backoff > 0:
            backoff = min(max_backoff, max(backoff * 1.5, backoff))


def ensure_unique_hosts(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for host in hosts:
        name = str(host["name"])
        if name in seen:
            continue
        seen.add(name)
        out.append(host)
    return out


def core_host(hosts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for host in hosts:
        if host["role"] == "k1s-core":
            return host
    return None


def ha_core_hosts(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [host for host in hosts if host["role"] == "k1s-ha-core"]


def lane_hosts(lane: str, hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    core = core_host(hosts)
    ha_cores = ha_core_hosts(hosts)
    if lane == "single_non_gpu":
        target = next((h for h in hosts if not h.get("gpu", False)), None)
        if target is None:
            return []
        selected = [target]
        if core is not None and core["name"] != target["name"]:
            selected.insert(0, core)
        return ensure_unique_hosts(selected)

    if lane == "single_gpu":
        target = next((h for h in hosts if h.get("gpu", False)), None)
        if target is None:
            return []
        selected = [target]
        if core is not None and core["name"] != target["name"]:
            selected.insert(0, core)
        return ensure_unique_hosts(selected)

    if lane == "multi_non_gpu":
        return ensure_unique_hosts([h for h in hosts if not h.get("gpu", False)])

    if lane == "multi_gpu":
        gpu_hosts = [h for h in hosts if h.get("gpu", False)]
        if not gpu_hosts:
            return []
        control = [h for h in hosts if h["role"] in {"k1s-core", "k1s-ha-core", "k1s-edge-core"}]
        return ensure_unique_hosts([*control, *gpu_hosts])

    if lane == "ha_control_plane":
        if not ha_cores:
            return []
        return ensure_unique_hosts(list(hosts))

    raise SmokeError(f"unsupported lane: {lane}")


def _ha_urls_for_host(host: dict[str, Any], variant: dict[str, Any]) -> tuple[str, str]:
    ha_cfg = variant.get("ha") if isinstance(variant.get("ha"), dict) else {}
    controller_scheme = str(ha_cfg.get("controller_scheme") or "http").strip()
    apishim_scheme = str(ha_cfg.get("apishim_scheme") or "http").strip()
    controller_port = int(variant["k1s"]["controller_port"])
    apishim_port = int(variant["k1s"].get("apishim_port", 8445))
    ip = str(host["ip"])
    return (
        f"{controller_scheme}://{ip}:{controller_port}",
        f"{apishim_scheme}://{ip}:{apishim_port}",
    )


def build_ha_lane_config(
    variant: dict[str, Any], lane_hosts_full: list[dict[str, Any]]
) -> dict[str, Any]:
    ha_cfg = variant.get("ha") if isinstance(variant.get("ha"), dict) else {}
    core_nodes = []
    for host in lane_hosts_full:
        if host["role"] != "k1s-ha-core":
            continue
        controller_url, apishim_url = _ha_urls_for_host(host, variant)
        core_nodes.append(
            {
                "name": host["name"],
                "node_id": host.get("node_id") or host["name"],
                "ip": host["ip"],
                "controller_url": controller_url,
                "apishim_url": apishim_url,
            }
        )
    controller_metrics_url = str(ha_cfg.get("controller_metrics_url") or "").strip()
    if not controller_metrics_url and core_nodes:
        controller_metrics_url = f"{core_nodes[0]['controller_url'].rstrip('/')}/metrics"
    build = ae_build_info()
    return {
        "core_nodes": core_nodes,
        "runtime_nodes": [
            {
                "name": host["name"],
                "node_id": host.get("node_id") or host["name"],
                "ip": host["ip"],
                "agent_url": f"http://{host['ip']}:{int(host.get('agent_port') or 9111)}",
                "labels": parse_label_csv(host.get("node_labels")),
            }
            for host in lane_hosts_full
            if host["role"] == "k1s-core-node"
        ],
        "edge_runtime_nodes": [
            {
                "name": host["name"],
                "node_id": host.get("node_id") or host["name"],
                "ip": host["ip"],
                "site_id": str(host.get("site_id") or "").strip(),
                "agent_url": f"http://{host['ip']}:{int(host.get('agent_port') or 9112)}",
                "labels": parse_label_csv(host.get("node_labels")),
            }
            for host in lane_hosts_full
            if host["role"] == "k1s-edge-node"
        ],
        "edge_core_sites": sorted(
            {
                str(host.get("site_id") or "").strip()
                for host in lane_hosts_full
                if host["role"] == "k1s-edge-core" and str(host.get("site_id") or "").strip()
            }
        ),
        "controller_metrics_url": controller_metrics_url,
        "etcd_endpoints": [str(item) for item in ha_cfg.get("etcd_endpoints") or []],
        "etcd_prefix": str(ha_cfg.get("etcd_prefix") or "").strip(),
        "nats_url": str(ha_cfg.get("nats_url") or "").strip(),
        "hub_nodes": list(ha_cfg.get("hub_nodes") or []),
        "edge_sites": list(ha_cfg.get("edge_sites") or []),
        "drills": dict(ha_cfg.get("drills") or {}),
        "expected_version": str(
            ha_cfg.get("expected_version") or build.get("version") or ""
        ).strip(),
        "expected_sha": str(ha_cfg.get("expected_sha") or "").strip(),
    }


def uses_vm_managed_ha_infra(variant: dict[str, Any], requested_lanes: list[str]) -> bool:
    if "ha_control_plane" not in requested_lanes:
        return False
    ha_cfg = variant.get("ha") if isinstance(variant.get("ha"), dict) else {}
    hosts = variant.get("hosts") if isinstance(variant.get("hosts"), list) else []
    core_hosts = [
        host for host in hosts if isinstance(host, dict) and host.get("role") == "k1s-ha-core"
    ]
    if len(core_hosts) != 3:
        return False
    core_ips = {
        str(host.get("ip") or "").strip()
        for host in core_hosts
        if str(host.get("ip") or "").strip()
    }
    if len(core_ips) != 3:
        return False
    etcd_hosts = {
        _url_host(item) for item in ha_cfg.get("etcd_endpoints") or [] if _url_host(str(item))
    }
    hub_hosts = {
        _url_host(str(node.get("monitor_url") or ""))
        for node in ha_cfg.get("hub_nodes") or []
        if isinstance(node, dict) and _url_host(str(node.get("monitor_url") or ""))
    }
    nats_host = _url_host(str(ha_cfg.get("nats_url") or ""))
    return bool(etcd_hosts == core_ips and hub_hosts == core_ips and nats_host in core_ips)


def parse_log_time(line: str) -> datetime | None:
    match = re.search(r"\]\s+(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)", line)
    if not match:
        return None
    raw = match.group(1)
    fmt = "%Y/%m/%d %H:%M:%S.%f" if "." in raw else "%Y/%m/%d %H:%M:%S"
    try:
        dt = datetime.strptime(raw, fmt)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)


def host_component_name(host: dict[str, Any]) -> str:
    role = host["role"]
    if role == "k1s-core":
        return "k1s-core-nats-hub"
    if role == "k1s-edge-core":
        site = str(host.get("site_id") or "").strip()
        return f"k1s-edge-nats-{site}" if site else "k1s-edge-nats"
    return ""


def host_service_check_command(
    host: dict[str, Any], controller_port: int, apishim_port: int
) -> str:
    role = host["role"]
    if role == "k1s-core":
        return (
            f"EP='{EP}'; "
            'CID=$(sudo crictl --runtime-endpoint "$EP" ps --label ae.stack.component=k1s-core-nats-hub -q | head -n1); '
            'if [ -z "$CID" ]; then CID=$(sudo crictl --runtime-endpoint "$EP" ps -a --label ae.stack.component=k1s-core-nats-hub -q | head -n1); fi; '
            "if [ -z \"$CID\" ]; then echo 'nats_hub_not_running'; exit 10; fi; "
            "if ! ss -ltn | awk '$4 ~ /:7422$/ {f=1} END {exit(f?0:1)}'; then echo 'leaf_port_7422_not_listening'; exit 11; fi; "
            f"if ! ss -ltn | awk '$4 ~ /:{controller_port}$/ {{f=1}} END {{exit(f?0:1)}}'; then "
            "echo 'controller_port_not_listening'; exit 12; fi; "
            "echo 'ok'"
        )

    if role == "k1s-ha-core":
        return (
            f"if ! ss -ltn | awk '$4 ~ /:{controller_port}$/ {{f=1}} END {{exit(f?0:1)}}'; then "
            "echo 'controller_port_not_listening'; exit 40; fi; "
            f"if ! ss -ltn | awk '$4 ~ /:{apishim_port}$/ {{f=1}} END {{exit(f?0:1)}}'; then "
            "echo 'apishim_port_not_listening'; exit 41; fi; "
            "echo 'ok'"
        )

    if role == "k1s-core-node":
        port = int(host.get("agent_port") or 9111)
        return (
            f"if ss -ltn | awk '$4 ~ /:{port}$/ {{f=1}} END {{exit(f?0:1)}}'; then echo 'ok'; exit 0; fi; "
            "if pgrep -f 'k1s-core-node|ae.node' >/dev/null 2>&1; then echo 'agent_process_running'; exit 0; fi; "
            "echo 'core_node_agent_not_ready'; exit 13"
        )

    if role == "k1s-edge-core":
        component = host_component_name(host)
        return (
            f"EP='{EP}'; "
            f'CID=$(sudo crictl --runtime-endpoint "$EP" ps --label ae.stack.component={component} -q | head -n1); '
            f'if [ -z "$CID" ]; then CID=$(sudo crictl --runtime-endpoint "$EP" ps -a --label ae.stack.component={component} -q | head -n1); fi; '
            "if [ -z \"$CID\" ]; then echo 'edge_nats_not_running'; exit 20; fi; "
            "echo 'ok'"
        )

    if role == "k1s-edge-node":
        port = int(host.get("agent_port") or 9112)
        return (
            f"if ss -ltn | awk '$4 ~ /:{port}$/ {{f=1}} END {{exit(f?0:1)}}'; then echo 'ok'; exit 0; fi; "
            "if pgrep -f 'k1s-edge-node|ae.runtime.agent' >/dev/null 2>&1; then echo 'agent_process_running'; exit 0; fi; "
            "echo 'edge_node_agent_not_ready'; exit 30"
        )

    return "echo 'unsupported_role'; exit 99"


def nats_hub_logs(core_ip: str, *, lines: int = 4000) -> tuple[str, str]:
    cmd = (
        f"EP='{EP}'; "
        "cids=$({ "
        'sudo crictl --runtime-endpoint "$EP" ps --label ae.stack.component=k1s-core-nats-hub -q; '
        'sudo crictl --runtime-endpoint "$EP" ps -a --label ae.stack.component=k1s-core-nats-hub -q; '
        'sudo crictl --runtime-endpoint "$EP" ps --name k1s-core-nats-hub -q; '
        'sudo crictl --runtime-endpoint "$EP" ps -a --name k1s-core-nats-hub -q; '
        'sudo crictl --runtime-endpoint "$EP" ps --name nats -q; '
        'sudo crictl --runtime-endpoint "$EP" ps -a --name nats -q; '
        'sudo crictl --runtime-endpoint "$EP" ps -a | '
        "awk 'NR>1 && ($0 ~ /k1s-core-nats-hub|[[:space:]]nats[[:space:]]|\\/nats:|\\/nats@/) {print $1}'; "
        "} 2>/dev/null | awk 'NF && !seen[$1]++'); "
        "if [ -z \"$cids\" ]; then echo '__NATS_LOG_STATUS__:cid_not_found' 1>&2; exit 0; fi; "
        "had_log_error=0; "
        "had_log_path=0; "
        "had_log_path_file=0; "
        "for cid in $cids; do "
        '  if out=$(sudo crictl --runtime-endpoint "$EP" logs "$cid" 2>/dev/null); then '
        '    if [ -n "$out" ]; then '
        f"      printf '%s\\n' \"$out\" | tail -n {lines}; "
        "      echo '__NATS_LOG_STATUS__:ok' 1>&2; "
        "      exit 0; "
        "    fi; "
        "  else "
        "    had_log_error=1; "
        "  fi; "
        '  log_path=$(sudo crictl --runtime-endpoint "$EP" inspect "$cid" 2>/dev/null '
        '    | sed -n \'s/.*"logPath"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p\' | head -n1); '
        '  if [ -z "$log_path" ]; then continue; fi; '
        "  had_log_path=1; "
        '  if ! sudo test -f "$log_path"; then continue; fi; '
        "  had_log_path_file=1; "
        f'  out=$(sudo tail -n {lines} "$log_path" 2>/dev/null || true); '
        '  if [ -n "$out" ]; then '
        "    printf '%s\\n' \"$out\"; "
        "    echo '__NATS_LOG_STATUS__:logpath_ok' 1>&2; "
        "    exit 0; "
        "  fi; "
        "done; "
        "if [ \"$had_log_error\" -eq 1 ]; then echo '__NATS_LOG_STATUS__:logs_cmd_failed' 1>&2; exit 0; fi; "
        'if [ "$had_log_path" -eq 1 ] && [ "$had_log_path_file" -eq 0 ]; then '
        "  echo '__NATS_LOG_STATUS__:logpath_missing' 1>&2; exit 0; "
        "fi; "
        "if [ \"$had_log_path_file\" -eq 1 ]; then echo '__NATS_LOG_STATUS__:logpath_empty' 1>&2; exit 0; fi; "
        "echo '__NATS_LOG_STATUS__:empty_logs' 1>&2; "
        "exit 0"
    )
    res = run_remote(core_ip, cmd, timeout=45)
    markers = re.findall(r"__NATS_LOG_STATUS__:([a-z_]+)", (res.stderr or ""))
    status = markers[-1] if markers else ("ssh_failed" if res.returncode != 0 else "unknown")
    logs = res.stdout or ""
    if logs.strip():
        return logs, status
    return "", status


def nats_edge_logs(edge: dict[str, Any], *, lines: int = 2500) -> tuple[str, str]:
    component = host_component_name(edge)
    if not component:
        return "", "unsupported_role"
    cmd = (
        f"EP='{EP}'; "
        f"cids=$({{ "
        f'sudo crictl --runtime-endpoint "$EP" ps --label ae.stack.component={component} -q; '
        f'sudo crictl --runtime-endpoint "$EP" ps -a --label ae.stack.component={component} -q; '
        f'sudo crictl --runtime-endpoint "$EP" ps --name {component} -q; '
        f'sudo crictl --runtime-endpoint "$EP" ps -a --name {component} -q; '
        "} 2>/dev/null | awk 'NF && !seen[$1]++'); "
        "if [ -z \"$cids\" ]; then echo '__NATS_EDGE_LOG_STATUS__:cid_not_found' 1>&2; exit 0; fi; "
        "had_log_error=0; "
        "had_log_path=0; "
        "had_log_path_file=0; "
        "for cid in $cids; do "
        '  if out=$(sudo crictl --runtime-endpoint "$EP" logs "$cid" 2>/dev/null); then '
        '    if [ -n "$out" ]; then '
        f"      printf '%s\\n' \"$out\" | tail -n {lines}; "
        "      echo '__NATS_EDGE_LOG_STATUS__:ok' 1>&2; "
        "      exit 0; "
        "    fi; "
        "  else "
        "    had_log_error=1; "
        "  fi; "
        '  log_path=$(sudo crictl --runtime-endpoint "$EP" inspect "$cid" 2>/dev/null '
        '    | sed -n \'s/.*"logPath"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p\' | head -n1); '
        '  if [ -z "$log_path" ]; then continue; fi; '
        "  had_log_path=1; "
        '  if ! sudo test -f "$log_path"; then continue; fi; '
        "  had_log_path_file=1; "
        f'  out=$(sudo tail -n {lines} "$log_path" 2>/dev/null || true); '
        '  if [ -n "$out" ]; then '
        "    printf '%s\\n' \"$out\"; "
        "    echo '__NATS_EDGE_LOG_STATUS__:logpath_ok' 1>&2; "
        "    exit 0; "
        "  fi; "
        "done; "
        "if [ \"$had_log_error\" -eq 1 ]; then echo '__NATS_EDGE_LOG_STATUS__:logs_cmd_failed' 1>&2; exit 0; fi; "
        'if [ "$had_log_path" -eq 1 ] && [ "$had_log_path_file" -eq 0 ]; then '
        "  echo '__NATS_EDGE_LOG_STATUS__:logpath_missing' 1>&2; exit 0; "
        "fi; "
        "if [ \"$had_log_path_file\" -eq 1 ]; then echo '__NATS_EDGE_LOG_STATUS__:logpath_empty' 1>&2; exit 0; fi; "
        "echo '__NATS_EDGE_LOG_STATUS__:empty_logs' 1>&2; "
        "exit 0"
    )
    res = run_remote(edge["ip"], cmd, timeout=35)
    markers = re.findall(r"__NATS_EDGE_LOG_STATUS__:([a-z_]+)", (res.stderr or ""))
    status = markers[-1] if markers else ("ssh_failed" if res.returncode != 0 else "unknown")
    logs = res.stdout or ""
    if logs.strip():
        return logs, status
    return "", status


def nats_signal_counts(lines: list[str], *, edge_ip: str = "") -> tuple[int, int, int]:
    scoped = lines
    if edge_ip:
        scoped = [line for line in lines if edge_ip in line]
    leaf_created = sum(1 for line in scoped if "Leafnode connection created" in line)
    jetstream_domains = sum(1 for line in scoped if "JetStream using domains" in line)
    auth_failures = sum(
        1
        for line in scoped
        if "authentication error" in line.lower() or "Authentication Failure" in line
    )
    return leaf_created, jetstream_domains, auth_failures


def fetch_leafz(core_ip: str) -> dict[str, Any] | None:
    cmd = (
        "python3 - <<'PY'\n"
        "import json, urllib.request\n"
        "payload = urllib.request.urlopen('http://127.0.0.1:8222/leafz', timeout=5).read().decode()\n"
        "print(payload)\n"
        "PY"
    )
    res = run_remote(core_ip, cmd, timeout=15)
    if res.returncode != 0:
        return None
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def leaf_count(leafz: dict[str, Any]) -> int:
    if "num_leafs" in leafz and isinstance(leafz["num_leafs"], int):
        return int(leafz["num_leafs"])
    if "num_leafnodes" in leafz and isinstance(leafz["num_leafnodes"], int):
        return int(leafz["num_leafnodes"])
    leafs = leafz.get("leafs")
    if isinstance(leafs, list):
        return len(leafs)
    return 0


def leaf_matches_edge(leaf: dict[str, Any], edge: dict[str, Any]) -> bool:
    edge_ip = str(edge.get("ip") or "").strip()
    leaf_ip = str(leaf.get("ip") or "").strip()
    if edge_ip and leaf_ip and edge_ip == leaf_ip:
        return True

    leaf_name = str(leaf.get("name") or "").strip().lower()
    if not leaf_name:
        return False

    aliases: set[str] = set()
    edge_site = str(edge.get("site_id") or "").strip().lower()
    edge_name = str(edge.get("name") or "").strip().lower()
    if edge_site:
        aliases.add(edge_site)
        aliases.add(f"edge-{edge_site}")
    if edge_name:
        aliases.add(edge_name)

    return any(alias and (leaf_name == alias or leaf_name.endswith(alias)) for alias in aliases)


def _first_output_line(stdout: str | None, stderr: str | None) -> str:
    for source in (stdout or "", stderr or ""):
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        if lines:
            return lines[0]
    return ""


def _run_helper_check(
    name: str,
    command: list[str],
    *,
    timeout_s: int,
    env: dict[str, str] | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    started = utc_now()
    try:
        res = run_cmd(command, timeout=timeout_s, check=False, env=env)
        ok = res.returncode == 0
        detail = "ok" if ok else (_first_output_line(res.stdout, res.stderr) or "command failed")
        payload = {
            "command": command,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "returncode": res.returncode,
        }
    except SmokeError as exc:
        ok = False
        detail = str(exc)
        payload = {"command": command, "error": str(exc)}
    ended = utc_now()
    return {
        "name": name,
        "status": "passed" if ok else "failed",
        "detail": detail,
        "optional": optional,
        "started_at": iso(started),
        "ended_at": iso(ended),
        "duration_s": round((ended - started).total_seconds(), 3),
        "payload": payload,
    }


def _skip_helper_check(name: str, detail: str, *, optional: bool = False) -> dict[str, Any]:
    now = utc_now()
    return {
        "name": name,
        "status": "skipped",
        "detail": detail,
        "optional": optional,
        "started_at": iso(now),
        "ended_at": iso(now),
        "duration_s": 0.0,
        "payload": {},
    }


def _ha_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    controller = (config.get("core_nodes") or [None])[0]
    if isinstance(controller, dict):
        env["AE_CONTROLLER_ID"] = str(controller.get("node_id") or controller.get("name") or "")
        env["AE_CONTROLLER_ADVERTISE_ADDR"] = str(controller.get("controller_url") or "")
    env["AE_STATE_BACKEND"] = str(env.get("AE_STATE_BACKEND") or "etcd")
    env["AE_HA_MODE"] = str(env.get("AE_HA_MODE") or "1")
    env["AE_JS_REPLICAS"] = str(env.get("AE_JS_REPLICAS") or "3")
    env["AE_ETCD_ENDPOINTS"] = ",".join(str(item) for item in config.get("etcd_endpoints") or [])
    env["AE_ETCD_PREFIX"] = str(config.get("etcd_prefix") or "")
    env["AE_NATS_URL"] = str(config.get("nats_url") or "")
    ca_bundle = ROOT / "state" / "profiles" / "k1s-ha-core" / "apishim.ca.crt"
    if ca_bundle.is_file():
        env["AE_APISHIM_CA_BUNDLE"] = str(ca_bundle)
    return env


def _ha_node_args(config: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for node in config.get("core_nodes") or []:
        args.extend(
            [
                "--node",
                f"{node['name']}={node['controller_url']},{node['apishim_url']}",
            ]
        )
    return args


def run_ha_acceptance_checks(config: dict[str, Any], *, timeout_s: int) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    env = _ha_env(config)
    scripts_dir = ROOT / "scripts" / "dev"

    if not config.get("core_nodes"):
        issues.append("no_k1s-ha-core_hosts")
    if not config.get("controller_metrics_url"):
        issues.append("missing_controller_metrics_url")
    if not config.get("etcd_endpoints"):
        issues.append("missing_etcd_endpoints")
    if not config.get("etcd_prefix"):
        issues.append("missing_etcd_prefix")
    if not config.get("nats_url"):
        issues.append("missing_nats_url")
    if not config.get("hub_nodes"):
        issues.append("missing_hub_nodes")
    edge_core_sites = set(config.get("edge_core_sites") or [])
    configured_sites = {
        str(site.get("site_id") or "").strip() for site in config.get("edge_sites") or []
    }
    missing_edge_sites = sorted(
        site for site in edge_core_sites if site and site not in configured_sites
    )
    for site_id in missing_edge_sites:
        issues.append(f"missing_edge_site_config:{site_id}")

    if issues:
        return {
            "status": "failed",
            "detail": "; ".join(issues),
            "checks": [_skip_helper_check("ha_acceptance", "; ".join(issues))],
        }

    checks.append(
        _run_helper_check(
            "ha_core_preflight",
            [sys.executable, str(scripts_dir / "ha_core_preflight.py")],
            timeout_s=timeout_s,
            env=env,
        )
    )
    checks.append(
        _run_helper_check(
            "ha_core_precheck",
            [
                sys.executable,
                str(scripts_dir / "ha_core_upgrade.py"),
                "precheck",
                "--metrics-url",
                str(config["controller_metrics_url"]),
                "--etcd-endpoints",
                ",".join(config["etcd_endpoints"]),
                "--etcd-prefix",
                str(config["etcd_prefix"]),
                "--nats-urls",
                str(config["nats_url"]),
            ],
            timeout_s=timeout_s,
            env=env,
        )
    )
    cluster_cmd = [
        sys.executable,
        str(scripts_dir / "ha_core_upgrade.py"),
        "cluster-verify",
        *_ha_node_args(config),
        "--expected-version",
        str(config["expected_version"]),
    ]
    if config.get("expected_sha"):
        cluster_cmd.extend(["--expected-sha", str(config["expected_sha"])])
    checks.append(
        _run_helper_check(
            "ha_core_cluster_verify",
            cluster_cmd,
            timeout_s=timeout_s,
            env=env,
        )
    )

    hub_cmd = [
        sys.executable,
        str(scripts_dir / "ha_transport_upgrade.py"),
        "precheck",
        "--controller-metrics-url",
        str(config["controller_metrics_url"]),
    ]
    for node in config.get("hub_nodes") or []:
        hub_cmd.extend(["--node", f"{node['name']}={node['monitor_url']}"])
    checks.append(
        _run_helper_check(
            "ha_hub_transport_precheck",
            hub_cmd,
            timeout_s=timeout_s,
            env=env,
        )
    )

    runtime_nodes = list(config.get("runtime_nodes") or [])
    if runtime_nodes:
        runtime_node = runtime_nodes[0]
        core_ingress_ip = str(config["core_nodes"][0].get("ip") or "").strip()
        if not core_ingress_ip:
            core_ingress_ip = _url_host(str(config["core_nodes"][0].get("controller_url") or ""))
        precheck_cmd = [
            sys.executable,
            str(scripts_dir / "ha_core_node_smoke.py"),
            "precheck",
            "--node-id",
            str(runtime_node["node_id"]),
        ]
        for key, value in sorted((runtime_node.get("labels") or {}).items()):
            precheck_cmd.extend(["--label", f"{key}={value}"])
        checks.append(
            _run_helper_check(
                "ha_attached_node_precheck",
                precheck_cmd,
                timeout_s=timeout_s,
                env=env,
            )
        )

        ingress_cmd = [
            sys.executable,
            str(scripts_dir / "ha_core_node_smoke.py"),
            "ingress-smoke",
            "--node-id",
            str(runtime_node["node_id"]),
            "--manifest",
            str(ROOT / "docs" / "site" / "examples" / "ha-web-smoke.yaml"),
            "--app-name",
            "ha-web-smoke",
            "--ingress-host",
            "ha-web-smoke.home.arpa",
            "--ingress-port",
            "10443",
            "--resolve-ip",
            core_ingress_ip,
            "--direct-probe-host",
            core_ingress_ip,
            "--health-path",
            "/healthz",
            "--root-path",
            "/",
            "--expected-text",
            "Shell + Port-Forward Smoke",
            "--timeout",
            str(timeout_s),
            "--poll",
            "2",
            "--purge-history",
        ]
        for key, value in sorted((runtime_node.get("labels") or {}).items()):
            ingress_cmd.extend(["--label", f"{key}={value}"])
        checks.append(
            _run_helper_check(
                "ha_attached_node_ingress_smoke",
                ingress_cmd,
                timeout_s=timeout_s,
                env=env,
            )
        )

    for site in config.get("edge_sites") or []:
        expected_gateways = [str(item) for item in site.get("expected_gateways") or []]
        precheck_cmd = [
            sys.executable,
            str(scripts_dir / "ha_edge_transport.py"),
            "precheck",
            "--site",
            f"{site['site_id']}={site['monitor_url']}",
            "--controller-metrics-url",
            str(config["controller_metrics_url"]),
        ]
        verify_cmd = [
            sys.executable,
            str(scripts_dir / "ha_edge_transport.py"),
            "site-verify",
            "--site",
            f"{site['site_id']}={site['monitor_url']}",
            "--controller-metrics-url",
            str(config["controller_metrics_url"]),
            "--require-gateway-converged",
        ]
        for gateway in expected_gateways:
            precheck_cmd.extend(["--expected-gateway", gateway])
            verify_cmd.extend(["--expected-gateway", gateway])
        checks.append(
            _run_helper_check(
                f"ha_edge_precheck:{site['site_id']}",
                precheck_cmd,
                timeout_s=timeout_s,
                env=env,
            )
        )
        checks.append(
            _run_helper_check(
                f"ha_edge_verify:{site['site_id']}",
                verify_cmd,
                timeout_s=timeout_s,
                env=env,
            )
        )

    edge_runtime_nodes = list(config.get("edge_runtime_nodes") or [])
    if edge_runtime_nodes and config.get("edge_sites"):
        edge_runtime = edge_runtime_nodes[0]
        edge_site = next(
            (
                site
                for site in config.get("edge_sites") or []
                if str(site.get("site_id") or "").strip()
                == str(edge_runtime.get("site_id") or "").strip()
            ),
            None,
        )
        target_probe_host = _url_host(
            edge_site.get("monitor_url") if isinstance(edge_site, dict) else ""
        )
        edge_runtime_ip = str(edge_runtime.get("ip") or "").strip()
        ingress_cmd = [
            sys.executable,
            str(scripts_dir / "ha_core_node_smoke.py"),
            "ingress-smoke",
            "--node-id",
            str(edge_runtime["node_id"]),
            "--manifest",
            str(ROOT / "docs" / "site" / "examples" / "ha-web-smoke-edge.yaml"),
            "--app-name",
            "ha-edge-web-smoke",
            "--ingress-host",
            "ha-edge-web-smoke.home.arpa",
            "--ingress-port",
            "10443",
            "--resolve-ip",
            str(config["core_nodes"][0]["ip"]),
            "--health-path",
            "/healthz",
            "--root-path",
            "/",
            "--expected-text",
            "Shell + Port-Forward Smoke",
            "--timeout",
            str(timeout_s),
            "--poll",
            "2",
            "--purge-history",
        ]
        if target_probe_host and edge_runtime_ip:
            ingress_cmd.extend(
                [
                    "--target-probe-host",
                    target_probe_host,
                    "--target-probe-user",
                    "ae",
                    "--target-probe-url",
                    f"http://{edge_runtime_ip}:18081/healthz",
                    "--target-probe-timeout",
                    "60",
                ]
            )
        for key, value in sorted((edge_runtime.get("labels") or {}).items()):
            ingress_cmd.extend(["--label", f"{key}={value}"])
        checks.append(
            _run_helper_check(
                f"ha_edge_runtime_ingress_smoke:{edge_runtime.get('site_id') or edge_runtime['node_id']}",
                ingress_cmd,
                timeout_s=timeout_s,
                env=env,
            )
        )

    drills = config.get("drills") if isinstance(config.get("drills"), dict) else {}
    leader_failover_command = str(drills.get("leader_failover_command") or "").strip()
    if leader_failover_command:
        checks.append(
            _run_helper_check(
                "ha_drill_leader_failover",
                [
                    sys.executable,
                    str(scripts_dir / "ha_core_drills.py"),
                    "leader-failover",
                    "--command",
                    leader_failover_command,
                    "--etcd-endpoints",
                    ",".join(config["etcd_endpoints"]),
                    "--etcd-prefix",
                    str(config["etcd_prefix"]),
                    "--require-controller-change",
                ],
                timeout_s=timeout_s,
                optional=True,
            )
        )
    else:
        checks.append(
            _skip_helper_check(
                "ha_drill_leader_failover",
                "not configured in variant.ha.drills.leader_failover_command",
                optional=True,
            )
        )

    etcd_restart_command = str(drills.get("etcd_restart_command") or "").strip()
    if etcd_restart_command:
        checks.append(
            _run_helper_check(
                "ha_drill_etcd_restart",
                [
                    sys.executable,
                    str(scripts_dir / "ha_core_drills.py"),
                    "etcd-restart",
                    "--command",
                    etcd_restart_command,
                    "--metrics-url",
                    str(config["controller_metrics_url"]),
                    "--etcd-endpoints",
                    ",".join(config["etcd_endpoints"]),
                    "--etcd-prefix",
                    str(config["etcd_prefix"]),
                ],
                timeout_s=timeout_s,
                optional=True,
            )
        )
    else:
        checks.append(
            _skip_helper_check(
                "ha_drill_etcd_restart",
                "not configured in variant.ha.drills.etcd_restart_command",
                optional=True,
            )
        )

    transport_recovery_command = str(drills.get("transport_recovery_command") or "").strip()
    first_site = next(iter(config.get("edge_sites") or []), None)
    if transport_recovery_command and isinstance(first_site, dict):
        checks.append(
            _run_helper_check(
                "ha_drill_transport_recovery",
                [
                    sys.executable,
                    str(scripts_dir / "ha_core_drills.py"),
                    "transport-recovery",
                    "--command",
                    transport_recovery_command,
                    "--metrics-url",
                    str(config["controller_metrics_url"]),
                    "--etcd-endpoints",
                    ",".join(config["etcd_endpoints"]),
                    "--etcd-prefix",
                    str(config["etcd_prefix"]),
                    "--site",
                    str(first_site["site_id"]),
                ],
                timeout_s=timeout_s,
                optional=True,
            )
        )
    else:
        checks.append(
            _skip_helper_check(
                "ha_drill_transport_recovery",
                "not configured in variant.ha.drills.transport_recovery_command",
                optional=True,
            )
        )

    required_failures = [
        check for check in checks if check["status"] == "failed" and not check["optional"]
    ]
    status = "failed" if required_failures else "passed"
    detail = "ok" if status == "passed" else "; ".join(check["name"] for check in required_failures)
    return {
        "status": status,
        "detail": detail,
        "checks": checks,
        "core_nodes": config["core_nodes"],
        "hub_nodes": config["hub_nodes"],
        "edge_sites": config["edge_sites"],
    }


def status_rank(status: str) -> int:
    if status == "failed":
        return 3
    if status == "passed":
        return 2
    if status == "skipped":
        return 1
    return 0


def lane_status_from_phases(phase_status: list[dict[str, Any]]) -> str:
    has_passed = False
    saw_phase = False
    for phase in phase_status:
        saw_phase = True
        status = str(phase.get("status", "")).strip().lower()
        if status == "failed":
            return "failed"
        if status == "passed":
            has_passed = True
    if has_passed:
        return "passed"
    if saw_phase:
        return "skipped"
    return "skipped"


def overall_status_from_lanes(lane_summaries: list[dict[str, Any]]) -> str:
    if any(str(lane.get("status", "")) == "failed" for lane in lane_summaries):
        return "failed"
    if lane_summaries and all(str(lane.get("status", "")) == "skipped" for lane in lane_summaries):
        return "skipped"
    return "passed"


def smoke_v2(args: argparse.Namespace) -> int:
    variant_path = Path(args.variant).resolve()
    if not variant_path.is_file():
        raise SmokeError(f"variant not found: {variant_path}")

    run_id = args.run_id or os.environ.get("RUN_ID") or default_run_id()
    output_root = (
        Path(args.output_root).expanduser().resolve() if args.output_root else ROOT / "runs"
    )
    run_dir = output_root / run_id
    lanes_dir = run_dir / "lanes"
    logs_dir = run_dir / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    lanes_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    variant = load_variant(variant_path)

    smoke_cfg = variant.get("smoke") if isinstance(variant.get("smoke"), dict) else {}
    smoke_defaults = (
        smoke_cfg.get("defaults") if isinstance(smoke_cfg.get("defaults"), dict) else {}
    )
    cfg_phase_timeouts = (
        smoke_defaults.get("phase_timeouts")
        if isinstance(smoke_defaults.get("phase_timeouts"), dict)
        else {}
    )

    phase_timeouts = dict(DEFAULT_PHASE_TIMEOUTS)
    for key in phase_timeouts:
        if key in cfg_phase_timeouts:
            phase_timeouts[key] = int(cfg_phase_timeouts[key])
    phase_timeouts.update(parse_phase_overrides(args.phase_timeout or []))

    retry_policy = dict(DEFAULT_RETRY_POLICY)
    cfg_retry = (
        smoke_defaults.get("retry_policy")
        if isinstance(smoke_defaults.get("retry_policy"), dict)
        else {}
    )
    for key in retry_policy:
        if key in cfg_retry:
            retry_policy[key] = float(cfg_retry[key])
    cli_retry = parse_retry_policy(args.retry_policy)
    retry_policy.update(cli_retry)

    default_lanes = (
        smoke_cfg.get("lanes") if isinstance(smoke_cfg.get("lanes"), list) else DEFAULT_LANES
    )
    requested_lanes = parse_csv(args.lanes) if args.lanes else [str(x) for x in default_lanes]
    vm_managed_ha_infra = uses_vm_managed_ha_infra(variant, requested_lanes)

    if args.down:
        args.auto_down_on_success = True
        args.auto_down_on_fail = True

    run_started = utc_now()
    lane_plans: list[dict[str, Any]] = []
    for lane_name in requested_lanes:
        if lane_name not in SUPPORTED_LANES:
            raise SmokeError(f"unknown lane: {lane_name}")
        selected_hosts = lane_hosts(lane_name, variant["hosts"])
        lane_plans.append(
            {
                "name": lane_name,
                "hosts": [
                    {"name": h["name"], "role": h["role"], "gpu": bool(h.get("gpu", False))}
                    for h in selected_hosts
                ],
                "host_count": len(selected_hosts),
                "skipped": len(selected_hosts) == 0,
                "skip_reason": "no matching hosts in variant" if not selected_hosts else "",
            }
        )

    plan_payload = {
        "run_id": run_id,
        "variant": variant_path.as_posix(),
        "variant_name": variant["name"],
        "profile": args.profile,
        "overlay": args.overlay or "",
        "run_started_at": iso(run_started),
        "phase_timeouts": phase_timeouts,
        "retry_policy": retry_policy,
        "checks": {
            "service_ready": True,
            "fabric_validate": True,
            "functional_basic": True,
            "functional_advanced": bool(parse_csv(args.enable_advanced_checks)),
            "ha_acceptance": True,
            "advanced_enabled": parse_csv(args.enable_advanced_checks),
            "disabled": parse_csv(args.disable_checks),
        },
        "lanes": lane_plans,
    }
    write_json(run_dir / "plan.json", plan_payload)

    if args.plan_only:
        summary = {
            "run_id": run_id,
            "variant_name": variant["name"],
            "status": "planned",
            "lanes": [
                {"name": lane["name"], "status": "skipped" if lane["skipped"] else "planned"}
                for lane in lane_plans
            ],
        }
        write_json(run_dir / "summary.json", summary)
        return 0

    global_phases: list[dict[str, Any]] = []

    def run_global_phase(
        phase: str,
        command: list[str],
        timeout_s: int,
        *,
        env: dict[str, str] | None = None,
    ) -> bool:
        started = utc_now()
        res = run_cmd(command, timeout=timeout_s, check=False, env=env)
        ended = utc_now()
        ok = res.returncode == 0
        stderr_tail = tail_lines(res.stderr)
        stdout_tail = tail_lines(res.stdout)
        detail = "ok" if ok else select_failure_detail(res.stderr, res.stdout)
        global_phases.append(
            {
                "phase": phase,
                "status": "passed" if ok else "failed",
                "started_at": iso(started),
                "ended_at": iso(ended),
                "duration_s": round((ended - started).total_seconds(), 3),
                "attempts": 1,
                "detail": detail,
                "command": command,
                "stderr_tail": stderr_tail,
                "stdout_tail": stdout_tail,
            }
        )
        return ok

    if args.profile != "local-vm":
        raise SmokeError("only --profile local-vm is implemented in smoke_v2")

    if not args.skip_up:
        up_ok = run_global_phase(
            "provision",
            [
                str(ROOT / "scripts" / "lab" / "vm" / "variant_up.sh"),
                "--variant",
                str(variant_path),
                "--run-id",
                run_id,
            ],
            timeout_s=phase_timeouts["provision"],
        )
        if not up_ok:
            write_json(run_dir / "global_phases.json", global_phases)
            write_json(
                run_dir / "summary.json",
                {
                    "run_id": run_id,
                    "variant_name": variant["name"],
                    "status": "failed",
                    "reason": "provision failed",
                    "global_phases": global_phases,
                },
            )
            return 1

    bootstrap_completed_at = utc_now()
    if not args.skip_bootstrap:
        seed_bundle_path = ROOT / "state" / "lab-vm" / run_id / "seeds" / CRI_SEED_BUNDLE_NAME
        seed_manifest_path = Path(
            os.environ.get("AE_CRI_CACHE_SEED_MANIFEST", str(CRI_SEED_MANIFEST))
        ).expanduser()
        seed_profile = os.environ.get("AE_CRI_CACHE_SEED_PROFILE", "all")
        seed_ok = run_global_phase(
            "seed_cache",
            [
                str(ROOT / "scripts" / "lab" / "vm" / "image_seed_bundle.sh"),
                "--run-id",
                run_id,
                "--manifest",
                str(seed_manifest_path),
                "--profile",
                seed_profile,
            ],
            timeout_s=phase_timeouts["seed_cache"],
        )
        if not seed_ok:
            write_json(run_dir / "global_phases.json", global_phases)
            write_json(
                run_dir / "summary.json",
                {
                    "run_id": run_id,
                    "variant_name": variant["name"],
                    "status": "failed",
                    "reason": "seed_cache failed",
                    "global_phases": global_phases,
                },
            )
            return 1

        if vm_managed_ha_infra:
            shared_infra_ok = run_global_phase(
                "ha_shared_infra",
                [
                    str(ROOT / "scripts" / "lab" / "vm" / "ha_shared_infra.sh"),
                    "--variant",
                    str(variant_path),
                    "--run-id",
                    run_id,
                    "--execute",
                ],
                timeout_s=phase_timeouts["ha_shared_infra"],
            )
            if not shared_infra_ok:
                write_json(run_dir / "global_phases.json", global_phases)
                write_json(
                    run_dir / "summary.json",
                    {
                        "run_id": run_id,
                        "variant_name": variant["name"],
                        "status": "failed",
                        "reason": "ha_shared_infra failed",
                        "global_phases": global_phases,
                    },
                )
                return 1

        bootstrap_env = dict(os.environ)
        bootstrap_env["AE_CRI_CACHE_SEED_MODE"] = os.environ.get(
            "AE_CRI_CACHE_SEED_MODE", "required"
        )
        bootstrap_env["AE_CRI_CACHE_SEED_MANIFEST"] = str(seed_manifest_path)
        bootstrap_env["AE_CRI_CACHE_SEED_BUNDLE"] = os.environ.get(
            "AE_CRI_CACHE_SEED_BUNDLE",
            str(seed_bundle_path),
        )
        bootstrap_ok = run_global_phase(
            "bootstrap",
            [
                str(ROOT / "scripts" / "lab" / "vm" / "k1s_bootstrap.sh"),
                "--variant",
                str(variant_path),
                "--run-id",
                run_id,
                "--execute",
            ],
            timeout_s=phase_timeouts["bootstrap"],
            env=bootstrap_env,
        )
        bootstrap_completed_at = utc_now()
        if not bootstrap_ok:
            write_json(run_dir / "global_phases.json", global_phases)
            write_json(
                run_dir / "summary.json",
                {
                    "run_id": run_id,
                    "variant_name": variant["name"],
                    "status": "failed",
                    "reason": "bootstrap failed",
                    "global_phases": global_phases,
                },
            )
            return 1

    write_json(run_dir / "global_phases.json", global_phases)

    disabled_checks = set(parse_csv(args.disable_checks))
    advanced_checks = parse_csv(args.enable_advanced_checks)

    lane_summaries: list[dict[str, Any]] = []

    for lane in lane_plans:
        lane_name = lane["name"]
        lane_path = lanes_dir / lane_name
        lane_path.mkdir(parents=True, exist_ok=True)
        phase_status: list[dict[str, Any]] = []
        check_outputs: dict[str, Any] = {}

        if lane["skipped"]:
            summary = {
                "name": lane_name,
                "status": "skipped",
                "reason": lane["skip_reason"],
                "phase_status": [],
                "hosts": lane["hosts"],
            }
            lane_summaries.append(summary)
            write_json(lane_path / "phase_status.json", [])
            write_json(lane_path / "summary.json", summary)
            continue

        hosts_by_name = {h["name"]: h for h in variant["hosts"]}
        lane_hosts_full = [hosts_by_name[h["name"]] for h in lane["hosts"]]
        is_ha_lane = lane_name == "ha_control_plane"

        # service_ready
        if "service_ready" in disabled_checks:
            phase_status.append(
                {
                    "phase": "service_ready",
                    "status": "skipped",
                    "detail": "disabled via --disable-checks",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["service_ready"] = {"status": "skipped", "hosts": []}
        else:
            svc_started = utc_now()
            host_results: list[dict[str, Any]] = []
            host_failures = 0
            host_service_timeout = max(
                60,
                int(phase_timeouts["service_ready"] / max(1, len(lane_hosts_full))),
            )
            for host in lane_hosts_full:
                command = host_service_check_command(
                    host,
                    int(variant["k1s"]["controller_port"]),
                    int(variant["k1s"].get("apishim_port", 8445)),
                )

                def _check_host(
                    host: dict[str, Any] = host, command: str = command
                ) -> tuple[bool, str, Any]:
                    ssh_res = run_remote(host["ip"], "echo up", timeout=10)
                    if ssh_res.returncode != 0:
                        return False, "ssh_unreachable", {"stderr": (ssh_res.stderr or "").strip()}
                    res = run_remote(host["ip"], command, timeout=25)
                    ok = res.returncode == 0
                    detail = (res.stdout or res.stderr or "").strip().splitlines()
                    msg = detail[-1] if detail else "ok" if ok else "service_not_ready"
                    return (
                        ok,
                        msg,
                        {"stdout": res.stdout, "stderr": res.stderr, "returncode": res.returncode},
                    )

                ok, attempts, detail, payload = with_retry(
                    _check_host,
                    timeout_s=host_service_timeout,
                    retry_policy=retry_policy,
                )
                if not ok:
                    host_failures += 1
                host_results.append(
                    {
                        "name": host["name"],
                        "ip": host["ip"],
                        "role": host["role"],
                        "status": "passed" if ok else "failed",
                        "attempts": attempts,
                        "detail": detail,
                        "payload": payload,
                    }
                )

            svc_ended = utc_now()
            svc_status = "failed" if host_failures else "passed"
            phase_status.append(
                {
                    "phase": "service_ready",
                    "status": svc_status,
                    "detail": f"host_failures={host_failures}",
                    "attempts": sum(r["attempts"] for r in host_results),
                    "started_at": iso(svc_started),
                    "ended_at": iso(svc_ended),
                    "duration_s": round((svc_ended - svc_started).total_seconds(), 3),
                }
            )
            check_outputs["service_ready"] = {
                "status": svc_status,
                "host_failures": host_failures,
                "hosts": host_results,
            }

        # fabric_validate
        if is_ha_lane:
            phase_status.append(
                {
                    "phase": "fabric_validate",
                    "status": "skipped",
                    "detail": "ha_control_plane lane uses ha_acceptance instead",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["fabric_validate"] = {
                "status": "skipped",
                "reason": "ha_control_plane lane",
            }
        elif "fabric_validate" in disabled_checks:
            phase_status.append(
                {
                    "phase": "fabric_validate",
                    "status": "skipped",
                    "detail": "disabled via --disable-checks",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["fabric_validate"] = {"status": "skipped"}
        else:
            core = next((h for h in lane_hosts_full if h["role"] == "k1s-core"), None)
            edges = [h for h in lane_hosts_full if h["role"] == "k1s-edge-core"]
            fab_started = utc_now()
            if core is None or not edges:
                fab_status = "skipped"
                fab_detail = "requires one k1s-core and at least one k1s-edge-core host"
                fab_attempts = 0
                fab_payload = {
                    "core_present": core is not None,
                    "edge_cores": [h["name"] for h in edges],
                    "status": "skipped",
                    "reason": fab_detail,
                }
            else:

                def _check_fabric(
                    core: dict[str, Any] = core,
                    edges: list[dict[str, Any]] = edges,
                    bootstrap_at: datetime = bootstrap_completed_at,
                ) -> tuple[bool, str, Any]:
                    logs, logs_reason = nats_hub_logs(core["ip"], lines=4500)
                    logs_available = bool(logs.strip())
                    failing_edges: list[dict[str, Any]] = []
                    signal_gaps: list[dict[str, Any]] = []
                    edge_log_status: dict[str, str] = {}
                    if logs_available:
                        recent_cutoff = bootstrap_at - timedelta(seconds=60)
                        filtered: list[str] = []
                        for line in logs.splitlines():
                            ts = parse_log_time(line)
                            if ts is None or ts >= recent_cutoff:
                                filtered.append(line)
                    else:
                        filtered = []

                    for edge in edges:
                        ip = edge["ip"]
                        created, js_ok, auth_fail = nats_signal_counts(filtered, edge_ip=ip)

                        edge_logs, edge_logs_reason = nats_edge_logs(edge, lines=2500)
                        edge_log_status[edge["name"]] = edge_logs_reason
                        if edge_logs.strip():
                            edge_filtered = edge_logs.splitlines()
                            edge_created, edge_js_ok, edge_auth_fail = nats_signal_counts(
                                edge_filtered
                            )
                            created = max(created, edge_created)
                            js_ok = max(js_ok, edge_js_ok)
                            auth_fail = max(auth_fail, edge_auth_fail)

                        edge_signal = {
                            "name": edge["name"],
                            "ip": ip,
                            "leaf_created": created,
                            "jetstream_domains": js_ok,
                            "auth_failures": auth_fail,
                        }
                        if auth_fail > 0:
                            failing_edges.append(edge_signal)
                            continue
                        if created <= 0 or js_ok <= 0:
                            signal_gaps.append(edge_signal)

                    leafz = fetch_leafz(core["ip"])
                    leafz_count = leaf_count(leafz) if isinstance(leafz, dict) else -1

                    if leafz_count >= 0 and leafz_count < len(edges):
                        failing_edges.append(
                            {
                                "name": "__leafz__",
                                "ip": "127.0.0.1",
                                "leaf_created": leafz_count,
                                "jetstream_domains": leafz_count,
                                "auth_failures": 0,
                                "expected_leafs": len(edges),
                            }
                        )

                    payload = {
                        "core": core["name"],
                        "edge_cores": [h["name"] for h in edges],
                        "leafz_count": leafz_count,
                        "logs_available": logs_available,
                        "logs_reason": logs_reason,
                        "failing_edges": failing_edges,
                        "signal_gaps": signal_gaps,
                        "edge_logs": edge_log_status,
                    }
                    if failing_edges:
                        return False, "fabric_not_stable", payload

                    # leafz is authoritative when available and healthy.
                    if leafz_count >= len(edges):
                        payload["signal_gaps"] = []
                        return True, "ok", payload

                    # If leafz is unavailable, fall back to log-only evidence.
                    if leafz_count < 0:
                        if signal_gaps:
                            return False, "leafz_unavailable_with_partial_log_signals", payload
                        if not logs_available and all(
                            reason in {"empty_logs", "cid_not_found", "ssh_failed", "unknown"}
                            for reason in edge_log_status.values()
                        ):
                            return False, "hub_and_edge_nats_logs_unavailable", payload
                        return True, "ok (logs-only)", payload

                    return True, "ok", payload

                ok, fab_attempts, fab_detail, fab_payload = with_retry(
                    _check_fabric,
                    timeout_s=phase_timeouts["fabric_validate"],
                    retry_policy=retry_policy,
                )
                fab_status = "passed" if ok else "failed"
            fab_ended = utc_now()
            phase_status.append(
                {
                    "phase": "fabric_validate",
                    "status": fab_status,
                    "detail": fab_detail,
                    "attempts": fab_attempts,
                    "started_at": iso(fab_started),
                    "ended_at": iso(fab_ended),
                    "duration_s": round((fab_ended - fab_started).total_seconds(), 3),
                }
            )
            check_outputs["fabric_validate"] = {
                "status": fab_status,
                "detail": fab_detail,
                "attempts": fab_attempts,
                "payload": fab_payload,
            }

        # functional_basic
        if is_ha_lane:
            phase_status.append(
                {
                    "phase": "functional_basic",
                    "status": "skipped",
                    "detail": "ha_control_plane lane uses ha_acceptance instead",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["functional_basic"] = {
                "status": "skipped",
                "reason": "ha_control_plane lane",
            }
        elif "functional_basic" in disabled_checks:
            phase_status.append(
                {
                    "phase": "functional_basic",
                    "status": "skipped",
                    "detail": "disabled via --disable-checks",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["functional_basic"] = {"status": "skipped"}
        else:
            fun_started = utc_now()
            core = next((h for h in lane_hosts_full if h["role"] == "k1s-core"), None)
            edges = [h for h in lane_hosts_full if h["role"] == "k1s-edge-core"]
            if core is None:
                fun_status = "skipped"
                fun_detail = "no k1s-core host in lane"
                fun_attempts = 0
                fun_payload = {"reason": fun_detail}
            else:

                def _check_functional(
                    core: dict[str, Any] = core,
                    edges: list[dict[str, Any]] = edges,
                ) -> tuple[bool, str, Any]:
                    leafz = fetch_leafz(core["ip"])
                    if not isinstance(leafz, dict):
                        return False, "leafz_unavailable", {}
                    count = leaf_count(leafz)
                    expected = len(edges)
                    payload = {"leafz_count": count, "expected": expected, "leafz": leafz}
                    if expected > 0 and count < expected:
                        return False, f"leafz_count_below_expected ({count}<{expected})", payload
                    return True, "ok", payload

                ok, fun_attempts, fun_detail, fun_payload = with_retry(
                    _check_functional,
                    timeout_s=phase_timeouts["functional_basic"],
                    retry_policy=retry_policy,
                )
                fun_status = "passed" if ok else "failed"
            fun_ended = utc_now()
            phase_status.append(
                {
                    "phase": "functional_basic",
                    "status": fun_status,
                    "detail": fun_detail,
                    "attempts": fun_attempts,
                    "started_at": iso(fun_started),
                    "ended_at": iso(fun_ended),
                    "duration_s": round((fun_ended - fun_started).total_seconds(), 3),
                }
            )
            check_outputs["functional_basic"] = {
                "status": fun_status,
                "detail": fun_detail,
                "attempts": fun_attempts,
                "payload": fun_payload,
            }

        # functional_advanced
        ha_summary_payload: dict[str, Any] | None = None
        if is_ha_lane:
            ha_started = utc_now()
            if "ha_acceptance" in disabled_checks:
                ha_status = "skipped"
                ha_detail = "disabled via --disable-checks"
                ha_attempts = 0
                ha_payload = {"status": "skipped"}
            else:
                ha_config = build_ha_lane_config(variant, lane_hosts_full)
                ha_payload = run_ha_acceptance_checks(
                    ha_config,
                    timeout_s=phase_timeouts["ha_acceptance"],
                )
                ha_status = str(ha_payload.get("status") or "failed")
                ha_detail = str(ha_payload.get("detail") or "")
                ha_attempts = len(ha_payload.get("checks") or [])
            ha_ended = utc_now()
            phase_status.append(
                {
                    "phase": "ha_acceptance",
                    "status": ha_status,
                    "detail": ha_detail,
                    "attempts": ha_attempts,
                    "started_at": iso(ha_started),
                    "ended_at": iso(ha_ended),
                    "duration_s": round((ha_ended - ha_started).total_seconds(), 3),
                }
            )
            ha_summary_payload = {
                "lane": lane_name,
                "status": ha_status,
                "detail": ha_detail,
                "checks": ha_payload.get("checks") if isinstance(ha_payload, dict) else [],
                "core_nodes": ha_payload.get("core_nodes") if isinstance(ha_payload, dict) else [],
                "hub_nodes": ha_payload.get("hub_nodes") if isinstance(ha_payload, dict) else [],
                "edge_sites": ha_payload.get("edge_sites") if isinstance(ha_payload, dict) else [],
            }
            check_outputs["ha_acceptance"] = ha_summary_payload
        else:
            phase_status.append(
                {
                    "phase": "ha_acceptance",
                    "status": "skipped",
                    "detail": "non-HA lane",
                    "attempts": 0,
                    "started_at": iso(utc_now()),
                    "ended_at": iso(utc_now()),
                    "duration_s": 0.0,
                }
            )
            check_outputs["ha_acceptance"] = {"status": "skipped", "reason": "non-HA lane"}

        # functional_advanced
        adv_started = utc_now()
        if not advanced_checks or "functional_advanced" in disabled_checks:
            adv_status = "skipped"
            adv_detail = "disabled"
            adv_attempts = 0
            adv_payload = {"requested": advanced_checks}
        else:
            adv_status = "failed"
            adv_detail = f"advanced checks not implemented: {', '.join(advanced_checks)}"
            adv_attempts = 1
            adv_payload = {"requested": advanced_checks}
        adv_ended = utc_now()
        phase_status.append(
            {
                "phase": "functional_advanced",
                "status": adv_status,
                "detail": adv_detail,
                "attempts": adv_attempts,
                "started_at": iso(adv_started),
                "ended_at": iso(adv_ended),
                "duration_s": round((adv_ended - adv_started).total_seconds(), 3),
            }
        )
        check_outputs["functional_advanced"] = {
            "status": adv_status,
            "detail": adv_detail,
            "payload": adv_payload,
        }

        for check_name, payload in check_outputs.items():
            write_json(lane_path / "checks" / f"{check_name}.json", payload)
        if ha_summary_payload is not None:
            write_json(lane_path / "ha_summary.json", ha_summary_payload)
        write_json(lane_path / "phase_status.json", phase_status)

        lane_status = lane_status_from_phases(phase_status)

        lane_summary = {
            "name": lane_name,
            "status": lane_status,
            "hosts": lane["hosts"],
            "phase_status": phase_status,
            "checks": {name: payload["status"] for name, payload in check_outputs.items()},
        }
        lane_summaries.append(lane_summary)
        write_json(lane_path / "summary.json", lane_summary)

    overall_status = overall_status_from_lanes(lane_summaries)

    summary_payload = {
        "run_id": run_id,
        "variant_name": variant["name"],
        "variant": str(variant_path),
        "status": overall_status,
        "run_started_at": iso(run_started),
        "run_ended_at": iso(utc_now()),
        "phase_timeouts": phase_timeouts,
        "retry_policy": retry_policy,
        "global_phases": global_phases,
        "lanes": lane_summaries,
    }
    write_json(run_dir / "summary.json", summary_payload)
    ha_lane_summary = next(
        (lane for lane in lane_summaries if lane["name"] == "ha_control_plane"), None
    )
    if ha_lane_summary is not None:
        lane_ha_summary = lanes_dir / "ha_control_plane" / "ha_summary.json"
        if lane_ha_summary.is_file():
            payload = json.loads(lane_ha_summary.read_text(encoding="utf-8"))
            write_json(
                run_dir / "ha_summary.json",
                {
                    "run_id": run_id,
                    "variant_name": variant["name"],
                    **payload,
                },
            )

    lines = [
        f"# Smoke Summary ({run_id})",
        "",
        f"- Variant: `{variant['name']}`",
        f"- Status: `{overall_status}`",
        "",
        "## Lanes",
    ]
    for lane in lane_summaries:
        lines.append(f"- `{lane['name']}`: `{lane['status']}`")
    lines.append("")
    lines.append("## Global Phases")
    for phase in global_phases:
        lines.append(f"- `{phase['phase']}`: `{phase['status']}` ({phase['duration_s']}s)")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    collect_cmd = [
        str(ROOT / "scripts" / "lab" / "vm" / "collect_baseline.sh"),
        "--variant",
        str(variant_path),
        "--run-id",
        run_id,
    ]
    if overall_status == "failed":
        run_cmd(collect_cmd, check=False)

    should_down = False
    if overall_status == "passed" and args.auto_down_on_success:
        should_down = True
    if overall_status == "failed" and args.auto_down_on_fail and not args.keep_on_fail:
        should_down = True

    if should_down:
        down_cmd = [
            str(ROOT / "scripts" / "lab" / "vm" / "variant_down.sh"),
            "--variant",
            str(variant_path),
            "--run-id",
            run_id,
        ]
        if args.purge:
            down_cmd.append("--purge")
        if args.destroy_network:
            down_cmd.append("--destroy-network")
        run_cmd(down_cmd, check=False)

    return 0 if overall_status in {"passed", "skipped"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VM smoke harness v2 (phased/lane-aware)")
    parser.add_argument("--variant", required=True, help="Path to variant yaml")
    parser.add_argument(
        "--run-id", default="", help="Run id (default: RUN_ID env or UTC timestamp)"
    )
    parser.add_argument(
        "--profile", default="local-vm", help="Execution profile (local-vm|remote-lab)"
    )
    parser.add_argument(
        "--overlay", default="", help="Optional overlay path (reserved for future use)"
    )
    parser.add_argument("--lanes", default="", help="Comma-separated lane override")
    parser.add_argument(
        "--phase-timeout",
        action="append",
        default=[],
        help="Phase timeout overrides: phase=seconds[,phase=seconds]",
    )
    parser.add_argument(
        "--retry-policy",
        default="",
        help="Retry policy preset (bounded|short|none) or path to json",
    )
    parser.add_argument(
        "--enable-advanced-checks",
        default="",
        help="Comma-separated advanced checks to enable",
    )
    parser.add_argument(
        "--disable-checks",
        default="",
        help="Comma-separated checks to disable",
    )
    parser.add_argument("--skip-up", action="store_true", help="Skip provisioning (variant up)")
    parser.add_argument("--skip-bootstrap", action="store_true", help="Skip bootstrap execution")
    parser.add_argument("--skip-validate", action="store_true", help="Skip lane validation phases")
    parser.add_argument("--plan-only", action="store_true", help="Only write plan artifacts")
    parser.add_argument("--output-root", default="", help="Override run artifacts root directory")
    parser.add_argument(
        "--down", action="store_true", help="Compatibility shortcut for auto teardown"
    )
    parser.add_argument(
        "--auto-down-on-success", action="store_true", help="Run variant down on success"
    )
    parser.add_argument(
        "--auto-down-on-fail", action="store_true", help="Run variant down on failure"
    )
    parser.add_argument("--keep-on-fail", dest="keep_on_fail", action="store_true", default=True)
    parser.add_argument("--no-keep-on-fail", dest="keep_on_fail", action="store_false")
    parser.add_argument("--purge", action="store_true", help="With teardown, purge state dir")
    parser.add_argument(
        "--destroy-network", action="store_true", help="With teardown, remove bridge/NAT"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.run_id:
        args.run_id = os.environ.get("RUN_ID") or default_run_id()
    if args.skip_validate:
        args.disable_checks = ",".join(
            [
                x
                for x in [
                    args.disable_checks,
                    "service_ready,fabric_validate,functional_basic,ha_acceptance,functional_advanced",
                ]
                if x
            ]
        )
    try:
        require_vm_host_prereqs(
            skip_up=bool(args.skip_up or args.plan_only),
            auto_down=bool(args.down or args.auto_down_on_success or args.auto_down_on_fail),
        )
        return smoke_v2(args)
    except Exception as exc:
        write_unhandled_failure_summary(args, args.run_id, exc)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"[lab-vm] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
