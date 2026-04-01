#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ae.controller.spec import app_key_for_manifest, load_manifest  # noqa: E402
from ae.controller.state import RegistryConflictError, state_store_from_env  # noqa: E402


@dataclass(frozen=True)
class ReadyWorkloadEndpoint:
    pod_name: str
    endpoint: str
    host: str
    port: int
    pod_cidr: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ha_core_node_smoke.py",
        description="Validate a workload-capable runtime node in the HA VM smoke lanes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    precheck = sub.add_parser("precheck", help="Wait for the expected runtime node to be Ready.")
    precheck.add_argument("--node-id", required=True)
    precheck.add_argument("--label", action="append", default=[])
    precheck.add_argument("--timeout", type=int, default=120)
    precheck.add_argument("--poll", type=float, default=2.0)

    smoke = sub.add_parser(
        "workload-smoke",
        help="Apply a runtime-node smoke workload, wait for it to be Ready, then clean it up.",
    )
    smoke.add_argument("--node-id", required=True)
    smoke.add_argument("--label", action="append", default=[])
    smoke.add_argument("--manifest", type=Path, required=True)
    smoke.add_argument("--app-name", default="ha-core-node-smoke")
    smoke.add_argument("--timeout", type=int, default=180)
    smoke.add_argument("--poll", type=float, default=2.0)
    smoke.add_argument("--purge-history", action="store_true")

    ingress = sub.add_parser(
        "ingress-smoke",
        help="Apply a runtime-node smoke workload, wait for readiness, then verify it through the HA ingress.",
    )
    ingress.add_argument("--node-id", required=True)
    ingress.add_argument("--label", action="append", default=[])
    ingress.add_argument("--manifest", type=Path, required=True)
    ingress.add_argument("--app-name", default="ha-web-smoke")
    ingress.add_argument("--timeout", type=int, default=180)
    ingress.add_argument("--poll", type=float, default=2.0)
    ingress.add_argument("--purge-history", action="store_true")
    ingress.add_argument("--ingress-host", required=True)
    ingress.add_argument("--ingress-port", type=int, default=10443)
    ingress.add_argument("--resolve-ip", required=True)
    ingress.add_argument("--health-path", default="/healthz")
    ingress.add_argument("--root-path", default="/")
    ingress.add_argument("--expected-text", default="Shell + Port-Forward Smoke")
    ingress.add_argument("--direct-probe-host")
    ingress.add_argument("--direct-probe-user", default="ae")
    ingress.add_argument("--target-probe-host")
    ingress.add_argument("--target-probe-user", default="ae")
    ingress.add_argument("--target-probe-url")
    ingress.add_argument("--target-probe-timeout", type=int, default=60)
    return parser.parse_args()


def parse_labels(items: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in items:
        raw = str(item or "").strip()
        if not raw or "=" not in raw:
            raise SystemExit(f"invalid --label value: {item!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"invalid --label value: {item!r}")
        labels[key] = value
    return labels


def ensure_ha_env() -> None:
    os.environ.setdefault("AE_STATE_BACKEND", "etcd")


def label_mismatches(actual: dict[str, Any], expected: dict[str, str]) -> list[str]:
    mismatches: list[str] = []
    for key, value in expected.items():
        if str(actual.get(key) or "") != value:
            mismatches.append(f"{key}={value}")
    return mismatches


def find_ready_node(store, node_id: str, expected_labels: dict[str, str]) -> tuple[bool, str]:
    for node, status in store.list_nodes():
        if node.node_id != node_id:
            continue
        missing = label_mismatches(node.labels or {}, expected_labels)
        if missing:
            return False, f"node labels mismatch: {', '.join(missing)}"
        if status is None:
            return False, f"node heartbeat missing: {node_id}"
        if str(status.status or "").strip().lower() != "ready":
            return False, f"node not ready: {node_id} status={status.status}"
        return True, f"node ready: {node_id}"
    return False, f"node not found: {node_id}"


def wait_for_node_ready(store, node_id: str, expected_labels: dict[str, str], *, timeout_s: int, poll_s: float) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"node not found: {node_id}"
    while True:
        ok, detail = find_ready_node(store, node_id, expected_labels)
        if ok:
            return detail
        last_detail = detail
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def load_smoke_manifest(path: Path, app_name: str):
    manifest = load_manifest(path)
    metadata = manifest.metadata.model_copy(update={"name": app_name})
    return manifest.model_copy(update={"metadata": metadata})


def apply_manifest(store, manifest) -> int:
    app_name = app_key_for_manifest(manifest)
    existing = store.get_registered_entry(app_name)
    source = existing.source if existing else "ha-core-node-smoke"
    labels = dict(existing.labels or {}) if existing else dict(getattr(manifest.metadata, "labels", {}) or {})
    labels.setdefault("ae.harness", "ha-core-node-smoke")
    return store.register_app(
        manifest,
        source=source,
        labels=labels,
        expected_resource_version=(existing.resource_version if existing else None),
    )


def wait_for_workload_ready(store, app_name: str, *, timeout_s: int, poll_s: float) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"workload not yet observed: {app_name}"
    while True:
        status = store.get_status(app_name)
        if status is not None:
            desired = int(status.desired_replicas or 0)
            ready = int(status.ready_replicas or 0)
            live = int(status.live_replicas or 0)
            if desired > 0 and ready >= desired and live >= desired:
                return (
                    f"workload ready: app={app_name} desired={desired} ready={ready} live={live}"
                )
            last_detail = (
                f"workload pending: app={app_name} desired={desired} ready={ready} live={live} "
                f"rev={status.revision} status={status.revision_status}"
            )
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def _endpoint_host(endpoint: str | None) -> str | None:
    raw = str(endpoint or "").strip()
    if not raw:
        return None
    if raw.startswith("[") and "]:" in raw:
        host, _port = raw.rsplit("]:", 1)
        return host.lstrip("[")
    if ":" not in raw:
        return None
    host, _port = raw.rsplit(":", 1)
    return host.strip() or None


def verify_workload_endpoint_cidr(store, app_name: str, node_id: str) -> str:
    endpoint = select_workload_endpoint(store, app_name, node_id)
    return format_workload_endpoint_detail(app_name, endpoint)


def format_workload_endpoint_detail(app_name: str, endpoint: ReadyWorkloadEndpoint) -> str:
    return (
        f"pod endpoint ok: app={app_name} pod={endpoint.pod_name} "
        f"endpoint={endpoint.endpoint} pod_cidr={endpoint.pod_cidr}"
    )


def select_workload_endpoint(store, app_name: str, node_id: str) -> ReadyWorkloadEndpoint:
    pod_cidr = None
    for node, _status in store.list_nodes():
        if node.node_id == node_id:
            pod_cidr = str(node.pod_cidr or "").strip() or None
            break
    if not pod_cidr:
        raise SystemExit(f"node pod CIDR missing: node={node_id}")

    try:
        network = ipaddress.ip_network(pod_cidr, strict=False)
    except ValueError as exc:
        raise SystemExit(f"invalid node pod CIDR: node={node_id} pod_cidr={pod_cidr}") from exc

    pods = store.list_pods(app_name)
    ready_hosts: list[tuple[str, str]] = []
    for pod in pods:
        if not pod.ready:
            continue
        host = _endpoint_host(pod.endpoint)
        if host is None:
            continue
        ready_hosts.append((pod.pod_name, str(pod.endpoint), host))
        try:
            endpoint_ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise SystemExit(
                f"pod endpoint is not an IP address: app={app_name} pod={pod.pod_name} endpoint={pod.endpoint}"
            ) from exc
        if endpoint_ip in network:
            port = _endpoint_port(str(pod.endpoint))
            if port is None:
                raise SystemExit(
                    f"pod endpoint missing port: app={app_name} pod={pod.pod_name} endpoint={pod.endpoint}"
                )
            return ReadyWorkloadEndpoint(
                pod_name=str(pod.pod_name),
                endpoint=str(pod.endpoint),
                host=host,
                port=port,
                pod_cidr=pod_cidr,
            )

    if not ready_hosts:
        raise SystemExit(f"workload ready but no ready pod endpoint recorded: app={app_name}")

    pod_name, endpoint, host = ready_hosts[0]
    raise SystemExit(
        f"pod endpoint outside node pod CIDR: app={app_name} pod={pod_name} "
        f"endpoint={endpoint} expected_cidr={pod_cidr}"
    )


def _endpoint_port(endpoint: str | None) -> int | None:
    raw = str(endpoint or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("[") and "]:" in raw:
            _host, port = raw.rsplit("]:", 1)
            return int(port)
        _host, port = raw.rsplit(":", 1)
        return int(port)
    except Exception:
        return None


def _ssh_identity_path() -> Path:
    key_path = Path(os.getenv("SSH_KEY_PATH") or (Path.home() / ".ssh" / "id_rsa"))
    if not key_path.exists():
        raise SystemExit(f"direct probe ssh key not found: {key_path}")
    return key_path


def _run_ssh_command(host: str, *, user: str, command: str, timeout_s: int) -> subprocess.CompletedProcess[str]:
    if shutil.which("ssh") is None:
        raise SystemExit("ssh is required for ingress-smoke direct probe")
    key_path = _ssh_identity_path()
    return subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            str(key_path),
            f"{user}@{host}",
            f"bash -lc {shlex.quote(command)}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=max(int(timeout_s), 5) + 5,
    )


def _collapse_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout, stderr) if str(part).strip()]
    return "\n".join(parts)


def _direct_probe_debug(
    *,
    probe_host: str,
    probe_user: str,
    endpoint: ReadyWorkloadEndpoint,
    timeout_s: int,
) -> str:
    debug_command = " ; ".join(
        [
            f"ip route get {shlex.quote(endpoint.host)} || true",
            "echo '---'",
            "ip route show || true",
            "echo '---'",
            "(networkctl status ens3 2>/dev/null || sudo networkctl status ens3 2>/dev/null || true)",
        ]
    )
    debug_result = _run_ssh_command(
        probe_host,
        user=probe_user,
        command=debug_command,
        timeout_s=timeout_s,
    )
    return _collapse_output(debug_result.stdout, debug_result.stderr)


def probe_direct_endpoint_via_ssh(
    *,
    probe_host: str,
    probe_user: str,
    endpoint: ReadyWorkloadEndpoint,
    path: str,
    timeout_s: int,
) -> str:
    normalized_path = str(path or "/").strip() or "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    route_result = _run_ssh_command(
        probe_host,
        user=probe_user,
        command=f"ip route get {shlex.quote(endpoint.host)}",
        timeout_s=timeout_s,
    )
    route_detail = _collapse_output(route_result.stdout, route_result.stderr)
    if route_result.returncode != 0:
        debug_detail = _direct_probe_debug(
            probe_host=probe_host,
            probe_user=probe_user,
            endpoint=endpoint,
            timeout_s=timeout_s,
        )
        raise RuntimeError(
            f"direct probe route lookup failed: core={probe_host} endpoint={endpoint.endpoint} "
            f"detail={route_detail or 'unknown'}\n{debug_detail}"
        )

    curl_result = _run_ssh_command(
        probe_host,
        user=probe_user,
        command=(
            "curl --noproxy '*' -sS -o /dev/null "
            f"-w '%{{http_code}}' -m {max(int(timeout_s), 5)} "
            f"{shlex.quote(f'http://{endpoint.endpoint}{normalized_path}')}"
        ),
        timeout_s=timeout_s,
    )
    status_text = (curl_result.stdout or "").strip()
    if curl_result.returncode == 0 and status_text == "200":
        return (
            f"direct probe ok: core={probe_host} endpoint={endpoint.endpoint} "
            f"path={normalized_path} status=200"
        )

    debug_detail = _direct_probe_debug(
        probe_host=probe_host,
        probe_user=probe_user,
        endpoint=endpoint,
        timeout_s=timeout_s,
    )
    curl_detail = _collapse_output(curl_result.stdout, curl_result.stderr)
    raise RuntimeError(
        f"direct probe failed: core={probe_host} endpoint={endpoint.endpoint} "
        f"path={normalized_path} status={status_text or 'unknown'} detail={curl_detail or 'unknown'}\n"
        f"{debug_detail}"
    )


def wait_for_direct_endpoint_response(
    *,
    probe_host: str,
    probe_user: str,
    endpoint: ReadyWorkloadEndpoint,
    path: str,
    timeout_s: int,
    poll_s: float,
) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"direct probe not yet ready: core={probe_host} endpoint={endpoint.endpoint}"
    while True:
        try:
            return probe_direct_endpoint_via_ssh(
                probe_host=probe_host,
                probe_user=probe_user,
                endpoint=endpoint,
                path=path,
                timeout_s=max(int(poll_s * 5), 5),
            )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc)
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def _target_probe_debug(
    *,
    probe_host: str,
    probe_user: str,
    timeout_s: int,
) -> str:
    debug_result = _run_ssh_command(
        probe_host,
        user=probe_user,
        command=(
            "ss -ltn | egrep ':(10080|10443|18080|18081|2333|4223|8223)' || true"
        ),
        timeout_s=timeout_s,
    )
    return _collapse_output(debug_result.stdout, debug_result.stderr)


def probe_target_url_via_ssh(
    *,
    probe_host: str,
    probe_user: str,
    url: str,
    timeout_s: int,
) -> str:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        raise RuntimeError("target probe URL is empty")

    curl_result = _run_ssh_command(
        probe_host,
        user=probe_user,
        command=(
            "curl --noproxy '*' -sS -o /dev/null "
            f"-w '%{{http_code}}' -m {max(int(timeout_s), 5)} "
            f"{shlex.quote(normalized_url)}"
        ),
        timeout_s=timeout_s,
    )
    status_text = (curl_result.stdout or "").strip()
    if curl_result.returncode == 0 and status_text == "200":
        return f"target probe ok: host={probe_host} url={normalized_url} status=200"

    debug_detail = _target_probe_debug(
        probe_host=probe_host,
        probe_user=probe_user,
        timeout_s=timeout_s,
    )
    curl_detail = _collapse_output(curl_result.stdout, curl_result.stderr)
    raise RuntimeError(
        f"target probe failed: host={probe_host} url={normalized_url} "
        f"status={status_text or 'unknown'} detail={curl_detail or 'unknown'}\n"
        f"{debug_detail}"
    )


def wait_for_target_probe_response(
    *,
    probe_host: str,
    probe_user: str,
    url: str,
    timeout_s: int,
    poll_s: float,
) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"target probe not yet ready: host={probe_host} url={url}"
    while True:
        try:
            return probe_target_url_via_ssh(
                probe_host=probe_host,
                probe_user=probe_user,
                url=url,
                timeout_s=max(int(poll_s * 5), 5),
            )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc)
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def cleanup_workload(store, app_name: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
    existing = store.get_registered_entry(app_name)
    if existing is not None:
        try:
            store.delete_registered_app(app_name, expected_resource_version=existing.resource_version)
        except RegistryConflictError:
            refreshed = store.get_registered_entry(app_name)
            if refreshed is not None:
                store.delete_registered_app(app_name, expected_resource_version=refreshed.resource_version)

    deadline = time.monotonic() + float(timeout_s)
    while True:
        if store.get_registered_entry(app_name) is None and not store.list_pods(app_name):
            if purge_history:
                store.delete_app_state(app_name, purge_history=True)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"cleanup timeout for {app_name}")
        time.sleep(max(poll_s, 0.1))


def curl_https_resolved(
    host: str,
    *,
    port: int,
    resolve_ip: str,
    path: str,
    timeout_s: int,
) -> tuple[int, str]:
    if shutil.which("curl") is None:
        raise SystemExit("curl is required for ingress-smoke")
    if not path.startswith("/"):
        path = f"/{path}"
    cmd = [
        "curl",
        "-sk",
        "--connect-timeout",
        str(max(timeout_s, 1)),
        "--max-time",
        str(max(timeout_s, 1)),
        "--resolve",
        f"{host}:{port}:{resolve_ip}",
        f"https://{host}:{port}{path}",
        "-w",
        "\n%{http_code}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stdout = proc.stdout or ""
    stderr = (proc.stderr or "").strip()
    if "\n" not in stdout:
        detail = stderr or stdout.strip() or f"curl exited {proc.returncode}"
        raise RuntimeError(f"curl did not return an HTTP status for https://{host}:{port}{path}: {detail}")
    body, status_text = stdout.rsplit("\n", 1)
    status_text = status_text.strip()
    if not status_text.isdigit():
        detail = stderr or stdout.strip() or f"curl exited {proc.returncode}"
        raise RuntimeError(f"curl returned an invalid HTTP status for https://{host}:{port}{path}: {detail}")
    return int(status_text), body


def wait_for_ingress_response(
    *,
    host: str,
    port: int,
    resolve_ip: str,
    path: str,
    timeout_s: int,
    poll_s: float,
    expected_status: int,
    expected_text: str | None = None,
) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"ingress not yet ready: host={host} path={path}"
    while True:
        try:
            status_code, body = curl_https_resolved(
                host,
                port=port,
                resolve_ip=resolve_ip,
                path=path,
                timeout_s=max(int(poll_s * 5), 5),
            )
            if status_code == expected_status:
                if expected_text and expected_text not in body:
                    body_preview = " ".join(body.split())
                    if len(body_preview) > 160:
                        body_preview = f"{body_preview[:157]}..."
                    last_detail = (
                        f"ingress content mismatch: host={host} path={path} "
                        f"status={status_code} missing={expected_text!r} body={body_preview!r}"
                    )
                else:
                    return (
                        f"ingress ok: host={host} path={path} status={status_code}"
                        + (f" matched={expected_text!r}" if expected_text else "")
                    )
            else:
                last_detail = (
                    f"ingress status mismatch: host={host} path={path} "
                    f"expected={expected_status} actual={status_code}"
                )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc)
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def run_precheck(args: argparse.Namespace) -> int:
    ensure_ha_env()
    store = state_store_from_env()
    labels = parse_labels(args.label)
    detail = wait_for_node_ready(
        store,
        args.node_id,
        labels,
        timeout_s=int(args.timeout),
        poll_s=float(args.poll),
    )
    print(detail)
    return 0


def run_workload_smoke(args: argparse.Namespace) -> int:
    ensure_ha_env()
    store = state_store_from_env()
    labels = parse_labels(args.label)
    wait_for_node_ready(
        store,
        args.node_id,
        labels,
        timeout_s=int(args.timeout),
        poll_s=float(args.poll),
    )
    manifest = load_smoke_manifest(args.manifest, args.app_name)
    app_name = app_key_for_manifest(manifest)
    cleanup_error: Exception | None = None

    try:
        rv = apply_manifest(store, manifest)
        detail = wait_for_workload_ready(
            store,
            app_name,
            timeout_s=int(args.timeout),
            poll_s=float(args.poll),
        )
        print(f"workload apply rv={rv}")
        print(detail)
        return 0
    finally:
        try:
            cleanup_workload(
                store,
                app_name,
                timeout_s=max(int(args.timeout), 30),
                poll_s=float(args.poll),
                purge_history=bool(args.purge_history),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_error = exc
        if cleanup_error is not None:
            raise SystemExit(f"cleanup failed for {app_name}: {cleanup_error}") from cleanup_error


def run_ingress_smoke(args: argparse.Namespace) -> int:
    ensure_ha_env()
    store = state_store_from_env()
    labels = parse_labels(args.label)
    wait_for_node_ready(
        store,
        args.node_id,
        labels,
        timeout_s=int(args.timeout),
        poll_s=float(args.poll),
    )
    manifest = load_smoke_manifest(args.manifest, args.app_name)
    app_name = app_key_for_manifest(manifest)
    cleanup_error: Exception | None = None

    try:
        rv = apply_manifest(store, manifest)
        workload_detail = wait_for_workload_ready(
            store,
            app_name,
            timeout_s=int(args.timeout),
            poll_s=float(args.poll),
        )
        endpoint_detail = verify_workload_endpoint_cidr(store, app_name, str(args.node_id))
        direct_detail = None
        direct_probe_host = str(getattr(args, "direct_probe_host", "") or "").strip()
        if direct_probe_host:
            endpoint = select_workload_endpoint(store, app_name, str(args.node_id))
            direct_detail = wait_for_direct_endpoint_response(
                probe_host=direct_probe_host,
                probe_user=str(getattr(args, "direct_probe_user", "ae") or "ae"),
                endpoint=endpoint,
                path=str(args.health_path),
                timeout_s=int(args.timeout),
                poll_s=float(args.poll),
            )
        target_probe_host = str(getattr(args, "target_probe_host", "") or "").strip()
        target_probe_url = str(getattr(args, "target_probe_url", "") or "").strip()
        target_detail = None
        if target_probe_host or target_probe_url:
            if not target_probe_host or not target_probe_url:
                raise SystemExit(
                    "target probe requires both --target-probe-host and --target-probe-url"
                )
            target_detail = wait_for_target_probe_response(
                probe_host=target_probe_host,
                probe_user=str(getattr(args, "target_probe_user", "ae") or "ae"),
                url=target_probe_url,
                timeout_s=max(int(getattr(args, "target_probe_timeout", 60) or 60), 5),
                poll_s=float(args.poll),
            )
        health_detail = wait_for_ingress_response(
            host=str(args.ingress_host),
            port=int(args.ingress_port),
            resolve_ip=str(args.resolve_ip),
            path=str(args.health_path),
            timeout_s=int(args.timeout),
            poll_s=float(args.poll),
            expected_status=200,
        )
        root_detail = wait_for_ingress_response(
            host=str(args.ingress_host),
            port=int(args.ingress_port),
            resolve_ip=str(args.resolve_ip),
            path=str(args.root_path),
            timeout_s=int(args.timeout),
            poll_s=float(args.poll),
            expected_status=200,
            expected_text=str(args.expected_text),
        )
        print(f"workload apply rv={rv}")
        print(workload_detail)
        print(endpoint_detail)
        if direct_detail:
            print(direct_detail)
        if target_detail:
            print(target_detail)
        print(health_detail)
        print(root_detail)
        return 0
    finally:
        try:
            cleanup_workload(
                store,
                app_name,
                timeout_s=max(int(args.timeout), 30),
                poll_s=float(args.poll),
                purge_history=bool(args.purge_history),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_error = exc
        if cleanup_error is not None:
            raise SystemExit(f"cleanup failed for {app_name}: {cleanup_error}") from cleanup_error


def main() -> int:
    args = parse_args()
    if args.cmd == "precheck":
        return run_precheck(args)
    if args.cmd == "workload-smoke":
        return run_workload_smoke(args)
    if args.cmd == "ingress-smoke":
        return run_ingress_smoke(args)
    raise SystemExit(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
