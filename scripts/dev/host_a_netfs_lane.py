#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / "state" / "host-a-gpu.env"
DEFAULT_APISHIM_ENV = ROOT / "state" / "profiles" / "k1s-core" / "apishim.env"
DEFAULT_CONTROLLER_ENV = ROOT / "state" / "profiles" / "k1s-core" / "controller.env"
DEFAULT_APISHIM_SERVER = "https://127.0.0.1:8445"
DEFAULT_LANE_LOG_DIR = ROOT / "state" / "host-a-netfs-lane"
ROOT_COMMAND_PATH = (
    "/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
PRESYNC_STALE_IMAGES = (
    "docker.io/vllm/vllm-openai:latest",
    "docker.io/vllm/vllm-openai:v0.6.2",
    "docker.io/rayproject/ray:latest",
)


class LaneError(RuntimeError):
    """Raised when the Host A lane cannot continue."""


@dataclass(frozen=True)
class HostAConfig:
    repo_root: Path
    env_file: Path
    connection_uri: str
    domain_name: str
    guest_user: str
    guest_repo: str
    guest_key: Path
    guest_port: int
    node_id: str
    state_root: Path
    overlay_dir: Path
    base_image: Path
    apishim_env: Path
    controller_env: Path
    apishim_server: str
    lane_log_dir: Path
    nfs_export_root: Path
    nfs_export_path: Path
    nfs_container_name: str
    nfs_export_path_in_guest: str
    nfs_permitted: str
    smoke_script: Path
    labctl_script: Path
    gpu_validator_script: Path
    apishim_kubectl_script: Path

    @property
    def state_dir(self) -> Path:
        return self.state_root / self.domain_name

    @property
    def overlay_path(self) -> Path:
        return self.overlay_dir / f"{self.domain_name}.qcow2"

    @property
    def seed_path(self) -> Path:
        return self.overlay_dir / f"{self.domain_name}-seed.iso"

    @property
    def base_image_sha(self) -> Path:
        return self.base_image.with_suffix(self.base_image.suffix + ".sha256")

    @property
    def base_image_meta(self) -> Path:
        return self.base_image.with_suffix(self.base_image.suffix + ".meta.json")

    @property
    def build_gpu_dir(self) -> Path:
        return self.repo_root / "artifacts" / "images" / "build-gpu"

    @property
    def controller_log(self) -> Path:
        return self.lane_log_dir / "controller.log"

    @property
    def controller_profile_lock(self) -> Path:
        return self.repo_root / "state" / "profiles" / "k1s-core" / ".profile.lock"

    @property
    def ips_json(self) -> Path:
        return self.lane_log_dir / "ips.json"

    @property
    def inventory_json(self) -> Path:
        return self.state_dir / "inventory.json"


def log(message: str) -> None:
    print(f"[host-a-netfs-lane] {message}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="host_a_netfs_lane.py",
        description=(
            "Bring up, rebuild, or stop the Host A strict-CRI + NFS smoke lane "
            "and optionally run the Host A NFS PVC smoke."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    resume = sub.add_parser("resume", help="Fast-resume the Host A lane, then optionally run the smoke.")
    add_common_lane_args(resume)
    resume.add_argument("--restart-controller", action="store_true")
    resume.add_argument("--skip-controller", action="store_true")
    resume.add_argument("--skip-node", action="store_true")
    resume.add_argument("--skip-sync", action="store_true")
    resume.add_argument("--smoke", action="store_true")

    rebuild = sub.add_parser(
        "rebuild",
        help="Purge/rebuild the Host A guest image + VM, bring the lane up, then optionally run the smoke.",
    )
    add_common_lane_args(rebuild)
    rebuild.add_argument("--skip-gpu-validate", action="store_true")
    rebuild.add_argument("--skip-sync", action="store_true")
    rebuild.add_argument("--skip-controller", action="store_true")
    rebuild.add_argument("--skip-node", action="store_true")
    rebuild.add_argument("--smoke", action="store_true")

    down = sub.add_parser("down", help="Stop the Host A controller stack and VM.")
    down.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    down.add_argument("--force-vm", action="store_true")
    down.add_argument("--purge-artifacts", action="store_true")
    down.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def add_common_lane_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--guest-key", type=Path, default=Path.home() / ".ssh" / "id_rsa")
    parser.add_argument("--guest-port", type=int, default=22)
    parser.add_argument("--guest-ip")
    parser.add_argument("--controller-host-ip")
    parser.add_argument("--apishim-env", type=Path, default=DEFAULT_APISHIM_ENV)
    parser.add_argument("--controller-env", type=Path, default=DEFAULT_CONTROLLER_ENV)
    parser.add_argument("--server", default=DEFAULT_APISHIM_SERVER)
    parser.add_argument("--guest-ip-timeout", type=int, default=150)
    parser.add_argument("--controller-health-timeout", type=int, default=120)
    parser.add_argument("--node-ready-timeout", type=int, default=120)
    parser.add_argument("--overlay-timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")


def load_config(args: argparse.Namespace) -> HostAConfig:
    env_values = load_simple_env_file(args.env_file)
    repo_root = ROOT
    env_file = args.env_file
    connection_uri = env_values.get("HOST_A_GPU_CONNECTION_URI", "qemu:///system")
    domain_name = env_values.get("HOST_A_GPU_DOMAIN_NAME", "k1s-core-a-gpu")
    guest_user = env_values.get("HOST_A_GPU_GUEST_USER", "ae")
    guest_repo = env_values.get("HOST_A_GPU_GUEST_REPO", "/home/ae/k1s")
    node_id = env_values.get("HOST_A_GPU_NODE_ID", "core-a--hub")
    state_root = resolve_path(env_values.get("HOST_A_GPU_STATE_ROOT", "state/libvirt-host-a"), repo_root)
    overlay_dir = resolve_path(env_values.get("HOST_A_GPU_OVERLAY_DIR", "~/VMs"), repo_root)
    base_image = resolve_path(
        env_values.get("HOST_A_GPU_BASE_IMAGE", "artifacts/images/ubuntu-22.04-k1s-gpu.qcow2"),
        repo_root,
    )
    lane_log_dir = DEFAULT_LANE_LOG_DIR
    return HostAConfig(
        repo_root=repo_root,
        env_file=env_file,
        connection_uri=connection_uri,
        domain_name=domain_name,
        guest_user=guest_user,
        guest_repo=guest_repo,
        guest_key=Path(args.guest_key).expanduser(),
        guest_port=int(args.guest_port),
        node_id=node_id,
        state_root=state_root,
        overlay_dir=overlay_dir,
        base_image=base_image,
        apishim_env=Path(args.apishim_env),
        controller_env=Path(args.controller_env),
        apishim_server=str(args.server),
        lane_log_dir=lane_log_dir,
        nfs_export_root=repo_root / "state" / "host-a-nfs-export",
        nfs_export_path=repo_root / "state" / "host-a-nfs-export" / "netfs",
        nfs_container_name="ae-host-a-nfs",
        nfs_export_path_in_guest="/netfs",
        nfs_permitted=r"192.168.29.0\/24",
        smoke_script=repo_root / "scripts" / "dev" / "host_a_netfs_smoke.sh",
        labctl_script=repo_root / "scripts" / "lab" / "vm" / "labctl.sh",
        gpu_validator_script=repo_root / "scripts" / "dev" / "gpu_guest_passthrough_validate.py",
        apishim_kubectl_script=repo_root / "scripts" / "dev" / "apishim_kubectl.sh",
    )


def load_simple_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_path(raw: str, repo_root: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = repo_root / path
    return path


def shell_join(parts: Sequence[str]) -> str:
    return shlex.join([str(part) for part in parts])


def run_cmd(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    cwd: Path | None = None,
    input_text: str | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    if dry_run:
        log(f"dry-run: {shell_join(cmd)}")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    return subprocess.run(
        [str(part) for part in cmd],
        check=check,
        text=True,
        input=input_text,
        capture_output=capture_output,
        cwd=cwd,
    )


def require_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise LaneError(f"required command not found: {name}")


def ensure_lane_dirs(config: HostAConfig) -> None:
    config.lane_log_dir.mkdir(parents=True, exist_ok=True)
    config.nfs_export_path.mkdir(parents=True, exist_ok=True)


def resolve_controller_host_ip(args: argparse.Namespace) -> str:
    if getattr(args, "controller_host_ip", None):
        return str(args.controller_host_ip)
    proc = subprocess.run(
        ["ip", "route", "get", "1.1.1.1"],
        check=True,
        text=True,
        capture_output=True,
    )
    parts = proc.stdout.split()
    for idx, token in enumerate(parts):
        if token == "src" and idx + 1 < len(parts):
            return parts[idx + 1]
    raise LaneError("could not determine controller host IP from `ip route get 1.1.1.1`")


def build_controller_start_command(config: HostAConfig, controller_host_ip: str) -> str:
    env_pairs = [
        ("HOME", "/root"),
        ("LOGNAME", "root"),
        ("PATH", ROOT_COMMAND_PATH),
        ("USER", "root"),
        ("AE_DEV_LOCAL", "1"),
        ("AE_INFERENCE_EXPERIMENTAL", "1"),
        ("AE_RUNTIME_BACKEND", "cri"),
        ("AE_INFRA_BACKEND", "cri"),
        ("AE_REMOTE_RUNTIME_ENSURE_TIMEOUT", "180"),
        ("AE_CRI_RUNTIME_HANDLER", "runc"),
        ("AE_APISHIM_MODE", "cri"),
        ("AE_AGENT_API_PORT", "9110"),
        ("AE_AGENT_API_TOKEN", "devtoken"),
        ("POSTGRES_PORT", "55432"),
        ("POSTGRES_BIND_IP", controller_host_ip),
        ("AE_APISHIM_DSN", "postgresql://shim:shim@127.0.0.1:55432/shim"),
        ("AE_APISHIM_ETCD_ENDPOINTS", "http://127.0.0.1:2379"),
        ("AE_STORAGE_NFS_SERVER", controller_host_ip),
        ("AE_STORAGE_NFS_PATH", config.nfs_export_path_in_guest),
        ("AE_STORAGE_NFS_HOSTPATH", str(config.nfs_export_path)),
    ]
    env_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in env_pairs)
    return (
        f"cd {shlex.quote(str(config.repo_root))} && "
        f"nohup sudo env -i {env_text} bash ./scripts/dev/run_profile.sh k1s-core "
        f"> {shlex.quote(str(config.controller_log))} 2>&1 </dev/null &"
    )


def build_guest_bootstrap_script(config: HostAConfig, controller_host_ip: str, guest_ip: str) -> str:
    controller_url = f"http://{controller_host_ip}:9110"
    apishim_dsn = f"postgresql://shim:shim@{controller_host_ip}:55432/shim"
    return f"""set -euo pipefail
guest_repo={shlex.quote(config.guest_repo)}
controller_url={shlex.quote(controller_url)}
guest_ip={shlex.quote(guest_ip)}
apishim_dsn={shlex.quote(apishim_dsn)}

print_package_diagnostics() {{
  echo "[guest-bootstrap] package processes:" >&2
  ps -eo pid=,ppid=,etimes=,state=,comm=,args= | grep -E 'apt|apt-get|dpkg|unattended' | grep -v grep >&2 || true
  echo "[guest-bootstrap] package services:" >&2
  systemctl --no-pager --full --lines=20 status apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null >&2 || true
  echo "[guest-bootstrap] package lock holders:" >&2
  if command -v fuser >/dev/null 2>&1; then
    sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock 2>/dev/null >&2 || true
  else
    echo "fuser unavailable" >&2
  fi
}}

package_manager_busy() {{
  if pgrep -x apt >/dev/null 2>&1 \\
    || pgrep -x apt-get >/dev/null 2>&1 \\
    || pgrep -x dpkg >/dev/null 2>&1; then
    return 0
  fi
  if command -v fuser >/dev/null 2>&1; then
    if sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}}

wait_for_apt_idle() {{
  local deadline=$((SECONDS + 300))
  while (( SECONDS < deadline )); do
    if package_manager_busy; then
      sleep 2
      continue
    fi
    return 0
  done
  echo "timed out waiting for apt/dpkg to become idle" >&2
  print_package_diagnostics
  return 1
}}

run_apt_get() {{
  local attempt output rc
  for attempt in $(seq 1 10); do
    wait_for_apt_idle
    if output="$(sudo DEBIAN_FRONTEND=noninteractive apt-get "$@" 2>&1)"; then
      printf '%s\\n' "$output"
      return 0
    fi
    rc=$?
    printf '%s\\n' "$output" >&2
    if printf '%s' "$output" | grep -Eq 'Could not get lock|Unable to lock directory|dpkg frontend is locked by another process|dpkg was interrupted'; then
      sleep 3
      continue
    fi
    return "$rc"
  done
  echo "apt-get $* failed after repeated lock retries" >&2
  return 1
}}

cd "$guest_repo"

run_apt_get update
run_apt_get install -y nfs-common

if sudo python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages; then
  sudo python3 -m pip install -r requirements.in --break-system-packages
else
  sudo python3 -m pip install -r requirements.in
fi

sudo pkill -f -- 'k1s-core-node|python -m ae\\.node' >/dev/null 2>&1 || true
sleep 2

nohup sudo env \\
  AE_RUNTIME_BACKEND=cri \\
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \\
  AE_CRI_RUNTIME_HANDLER=nvidia \\
  AE_ENABLE_NETFS=1 \\
  AE_APISHIM_DSN="$apishim_dsn" \\
  AE_NODE_ID={shlex.quote(config.node_id)} \\
  AE_NODE_LABELS="role=hub,site=core-a,gpu.sku=titan-rtx" \\
  AE_NODE_ADVERTISE_IP="${{guest_ip}}" \\
  AE_POD_CIDR=10.42.0.0/24 \\
  AE_CNI_SUBNET=10.42.0.0/24 \\
  AE_ROSENPASS_ENABLED=0 \\
  AE_CONTROLLER_URL="$controller_url" \\
  AE_AGENT_ENDPOINT="http://${{guest_ip}}:9111" \\
  AE_AGENT_TOKEN=devtoken \\
  AE_NODE_PORT=9111 \\
  make k1s-core-node > /home/ae/k1s-core-node.log 2>&1 </dev/null &
"""


def build_smoke_command(config: HostAConfig, guest_ip: str) -> list[str]:
    return [
        "bash",
        str(config.smoke_script),
        "--guest-ip",
        guest_ip,
        "--guest-user",
        config.guest_user,
        "--guest-key",
        str(config.guest_key),
        "--guest-port",
        str(config.guest_port),
        "--apishim-env",
        str(config.apishim_env),
        "--controller-env",
        str(config.controller_env),
        "--server",
        config.apishim_server,
    ]


def start_vm(config: HostAConfig, *, dry_run: bool) -> None:
    proc = run_cmd(
        [str(config.labctl_script), "host-a-gpu", "start"],
        check=False,
        capture_output=True,
        cwd=config.repo_root,
        dry_run=dry_run,
    )
    if dry_run:
        return
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return
    if "Domain is already active" in output:
        log("VM already active")
        return
    raise LaneError(output.strip() or "failed to start Host A guest")


def wait_for_guest_ip(config: HostAConfig, *, timeout_s: int, dry_run: bool) -> str:
    if dry_run:
        placeholder = "192.168.29.104"
        log(f"dry-run: resolved guest_ip={placeholder}")
        return placeholder

    deadline = time.monotonic() + float(timeout_s)
    last_error = "guest IP not reported yet"
    while time.monotonic() < deadline:
        proc = run_cmd(
            [str(config.labctl_script), "host-a-gpu", "ips", "--json"],
            check=False,
            capture_output=True,
            cwd=config.repo_root,
        )
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            guest_ip = str(payload.get("primary_ip") or "").strip()
            if guest_ip:
                config.ips_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                return guest_ip
        last_error = ((proc.stderr or "") or (proc.stdout or "")).strip() or last_error
        time.sleep(5)

    domstate = run_cmd(
        ["virsh", "-c", config.connection_uri, "domstate", config.domain_name, "--reason"],
        check=False,
        capture_output=True,
    )
    detail = ((domstate.stdout or "") + (domstate.stderr or "")).strip()
    raise LaneError(f"{last_error}; domstate={detail or 'unknown'}")


def ensure_nfs_export(config: HostAConfig, *, dry_run: bool) -> None:
    ensure_lane_dirs(config)
    run_cmd(
        ["docker", "rm", "-f", config.nfs_container_name],
        check=False,
        capture_output=True,
        dry_run=dry_run,
    )
    run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            config.nfs_container_name,
            "--privileged",
            "--network",
            "host",
            "-e",
            "SHARED_DIRECTORY=/exports",
            "-e",
            f"PERMITTED={config.nfs_permitted}",
            "-v",
            f"{config.nfs_export_root}:/exports",
            "itsthenetwork/nfs-server-alpine:latest",
        ],
        cwd=config.repo_root,
        dry_run=dry_run,
    )


def controller_healthy() -> bool:
    try:
        payload = http_json("http://127.0.0.1:9110/healthz")
    except Exception:
        return False
    return payload == {"ok": True}


def start_controller(config: HostAConfig, controller_host_ip: str, *, dry_run: bool) -> None:
    ensure_lane_dirs(config)
    command = build_controller_start_command(config, controller_host_ip)
    run_cmd(["bash", "-lc", command], cwd=config.repo_root, dry_run=dry_run)


def tcp_connectable(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def profile_lock_available(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


def controller_log_tail(config: HostAConfig, *, lines: int = 40) -> str:
    if not config.controller_log.exists():
        return ""
    return "".join(config.controller_log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])


def controller_start_failure(config: HostAConfig) -> str | None:
    tail = controller_log_tail(config)
    if "profile 'k1s-core' is already running" in tail:
        return tail.strip()
    if "make: ***" in tail and "k1s-core-cri" in tail:
        return tail.strip()
    return None


def wait_for_controller_shutdown(config: HostAConfig, controller_host_ip: str, *, timeout_s: int, dry_run: bool) -> None:
    if dry_run:
        log(
            "dry-run: controller shutdown would be checked for "
            "lock release and port drain"
        )
        return
    deadline = time.monotonic() + float(timeout_s)
    last_state = "controller stack still shutting down"
    while time.monotonic() < deadline:
        lock_free = profile_lock_available(config.controller_profile_lock)
        health_up = controller_healthy()
        postgres_up = tcp_connectable(controller_host_ip, 55432)
        if lock_free and not health_up and not postgres_up:
            return
        last_state = (
            "controller shutdown incomplete: "
            f"lock_free={lock_free} health_up={health_up} postgres_up={postgres_up}"
        )
        time.sleep(2)
    tail = controller_log_tail(config)
    raise LaneError(f"timed out waiting for controller shutdown: {last_state}\n{tail}".strip())


def wait_for_controller_health(config: HostAConfig, controller_host_ip: str, *, timeout_s: int, dry_run: bool) -> None:
    if dry_run:
        log(
            "dry-run: controller health would be checked at "
            f"http://127.0.0.1:9110/healthz and tcp://{controller_host_ip}:55432"
        )
        return
    deadline = time.monotonic() + float(timeout_s)
    last_error = "controller health not yet green"
    while time.monotonic() < deadline:
        start_failure = controller_start_failure(config)
        if start_failure:
            raise LaneError(start_failure)
        try:
            payload = http_json("http://127.0.0.1:9110/healthz")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(2)
            continue
        if payload == {"ok": True} and tcp_connectable(controller_host_ip, 55432):
            return
        if payload != {"ok": True}:
            last_error = f"unexpected health payload: {payload!r}"
        else:
            last_error = f"controller health ready but postgres not yet reachable at {controller_host_ip}:55432"
        time.sleep(2)
    tail = controller_log_tail(config)
    raise LaneError(f"{last_error}\n{tail}".strip())


def sync_repo(config: HostAConfig, guest_ip: str, *, dry_run: bool) -> None:
    ssh_args = ssh_base_args(config)
    run_cmd(
        [*ssh_args, f"{config.guest_user}@{guest_ip}", "mkdir", "-p", config.guest_repo],
        cwd=config.repo_root,
        dry_run=dry_run,
    )
    rsync_cmd = [
        "rsync",
        "-az",
        "--delete",
        "--exclude",
        ".git/",
        "--exclude",
        ".venv/",
        "--exclude",
        "state/",
        "--exclude",
        "runs/",
        "--exclude",
        "artifacts/",
        "--exclude",
        "docs/site/",
        "--exclude",
        "__pycache__/",
        "--exclude",
        ".pytest_cache/",
        "--exclude",
        ".mypy_cache/",
        "-e",
        shell_join(ssh_args),
        "./",
        f"{config.guest_user}@{guest_ip}:{config.guest_repo}/",
    ]
    run_cmd(rsync_cmd, cwd=config.repo_root, dry_run=dry_run)


def build_guest_presync_cleanup_script() -> str:
    image_lines = "\n".join(f'for_image {shlex.quote(image)}' for image in PRESYNC_STALE_IMAGES)
    return f"""set -euo pipefail
echo "[guest-presync-cleanup] before:"
df -h / /tmp /var/lib/containerd || true
sudo rm -f /tmp/*-core-seed-cri-seed-images.oci.tar || true

for_image() {{
  local image="$1"
  local ids=""
  ids="$(sudo crictl ps -a --image "$image" -q 2>/dev/null || true)"
  if [[ -n "$ids" ]]; then
    sudo crictl rm -f $ids >/dev/null 2>&1 || true
  fi
  sudo ctr -n k8s.io images rm --sync "$image" >/dev/null 2>&1 || true
}}

{image_lines}

avail_kb="$(df -Pk / | awk 'NR==2 {{print $4}}')"
if [[ -n "$avail_kb" && "$avail_kb" -lt 4194304 ]]; then
  echo "[guest-presync-cleanup] low disk after targeted cleanup, purging CRI namespace"
  all_ids="$(sudo crictl ps -a -q 2>/dev/null || true)"
  if [[ -n "$all_ids" ]]; then
    sudo crictl rm -f $all_ids >/dev/null 2>&1 || true
  fi
  all_refs="$(sudo ctr -n k8s.io images ls -q 2>/dev/null || true)"
  if [[ -n "$all_refs" ]]; then
    sudo ctr -n k8s.io images rm --sync $all_refs >/dev/null 2>&1 || true
  fi
fi

echo "[guest-presync-cleanup] after:"
df -h / /tmp /var/lib/containerd || true
sudo du -sh /var/lib/containerd 2>/dev/null || true
"""


def presync_guest_cleanup(config: HostAConfig, guest_ip: str, *, dry_run: bool) -> None:
    script = build_guest_presync_cleanup_script()
    run_remote_script(config, guest_ip, script, dry_run=dry_run)


def ssh_base_args(config: HostAConfig) -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-i",
        str(config.guest_key),
        "-p",
        str(config.guest_port),
    ]


def run_remote_script(config: HostAConfig, guest_ip: str, script: str, *, dry_run: bool) -> None:
    cmd = [
        *ssh_base_args(config),
        f"{config.guest_user}@{guest_ip}",
        "bash",
        "-s",
        "--",
    ]
    run_cmd(cmd, input_text=script, dry_run=dry_run)


def guest_log_tail(config: HostAConfig, guest_ip: str, *, dry_run: bool) -> str:
    if dry_run:
        return ""
    proc = run_cmd(
        [
            *ssh_base_args(config),
            f"{config.guest_user}@{guest_ip}",
            "tail",
            "-n",
            "200",
            "/home/ae/k1s-core-node.log",
        ],
        check=False,
        capture_output=True,
    )
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def wait_for_guest_bootstrap_ready(config: HostAConfig, guest_ip: str, *, timeout_s: int, dry_run: bool) -> None:
    if dry_run:
        log(
            "dry-run: guest bootstrap readiness would wait for "
            "cloud-init completion and apt/dpkg idleness"
        )
        return
    script = f"""set -euo pipefail
deadline=$((SECONDS + {int(timeout_s)}))
cloud_init_done=0

print_bootstrap_diagnostics() {{
  echo "[guest-bootstrap-ready] cloud-init status:" >&2
  if command -v cloud-init >/dev/null 2>&1; then
    cloud-init status --long 2>/dev/null >&2 || cloud-init status 2>/dev/null >&2 || true
  else
    echo "cloud-init command unavailable" >&2
  fi
  echo "[guest-bootstrap-ready] system state:" >&2
  systemctl is-system-running 2>/dev/null >&2 || true
  echo "[guest-bootstrap-ready] package processes:" >&2
  ps -eo pid=,ppid=,etimes=,state=,comm=,args= | grep -E 'apt|apt-get|dpkg|unattended' | grep -v grep >&2 || true
  echo "[guest-bootstrap-ready] package services:" >&2
  systemctl --no-pager --full --lines=20 status apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null >&2 || true
  echo "[guest-bootstrap-ready] package lock holders:" >&2
  if command -v fuser >/dev/null 2>&1; then
    sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock 2>/dev/null >&2 || true
  else
    echo "fuser unavailable" >&2
  fi
  echo "[guest-bootstrap-ready] recent logs:" >&2
  tail -n 40 /var/log/cloud-init.log /var/log/cloud-init-output.log /var/log/apt/term.log 2>/dev/null >&2 || true
}}

package_manager_busy() {{
  if pgrep -x apt >/dev/null 2>&1 \\
    || pgrep -x apt-get >/dev/null 2>&1 \\
    || pgrep -x dpkg >/dev/null 2>&1; then
    return 0
  fi
  if command -v fuser >/dev/null 2>&1; then
    if sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}}

if command -v cloud-init >/dev/null 2>&1; then
  while (( SECONDS < deadline )); do
    status="$(cloud-init status 2>/dev/null || true)"
    if [[ "$status" == *"status: done"* ]] || [[ "$status" == *"status: disabled"* ]] || [[ -f /var/lib/cloud/instance/boot-finished ]]; then
      cloud_init_done=1
      break
    fi
    if [[ "$status" == *"status: error"* ]]; then
      printf '%s\\n' "$status" >&2
      print_bootstrap_diagnostics
      exit 1
    fi
    sleep 2
  done
  if (( cloud_init_done == 0 )) && [[ ! -f /var/lib/cloud/instance/boot-finished ]]; then
    echo "cloud-init did not finish before bootstrap deadline" >&2
    print_bootstrap_diagnostics
    exit 1
  fi
fi

while (( SECONDS < deadline )); do
  if package_manager_busy; then
    sleep 2
    continue
  fi
  exit 0
done

echo "guest package manager still busy before bootstrap deadline" >&2
print_bootstrap_diagnostics
exit 1
"""
    run_remote_script(config, guest_ip, script, dry_run=dry_run)


def verify_controller_from_guest(config: HostAConfig, guest_ip: str, controller_host_ip: str, *, dry_run: bool) -> None:
    script = f"""set -euo pipefail
curl -fsS http://{controller_host_ip}:9110/healthz >/dev/null
bash -lc 'cat </dev/null >/dev/tcp/{controller_host_ip}/55432'
"""
    run_remote_script(config, guest_ip, script, dry_run=dry_run)


def start_guest_node(config: HostAConfig, guest_ip: str, controller_host_ip: str, *, dry_run: bool) -> None:
    script = build_guest_bootstrap_script(config, controller_host_ip, guest_ip)
    run_remote_script(config, guest_ip, script, dry_run=dry_run)


def wait_for_guest_node(config: HostAConfig, guest_ip: str, controller_host_ip: str, *, node_timeout_s: int, overlay_timeout_s: int, dry_run: bool) -> None:
    if dry_run:
        log(f"dry-run: guest node API would be checked at http://{guest_ip}:9111/v1/containers")
        log(
            "dry-run: overlay registration would be checked at "
            f"http://{controller_host_ip}:9110/v1/nodes/{config.node_id}/overlay"
        )
        return
    wait_for_http_json(f"http://{guest_ip}:9111/v1/containers", timeout_s=node_timeout_s)
    wait_for_overlay_clear(controller_host_ip, config.node_id, timeout_s=overlay_timeout_s)


def run_smoke(config: HostAConfig, guest_ip: str, *, dry_run: bool) -> None:
    run_cmd(build_smoke_command(config, guest_ip), cwd=config.repo_root, dry_run=dry_run)


def rebuild_guest(config: HostAConfig, *, dry_run: bool) -> str:
    ensure_lane_dirs(config)
    run_cmd([str(config.labctl_script), "host-a-gpu", "stop", "--force"], check=False, cwd=config.repo_root, dry_run=dry_run)
    run_cmd([str(config.labctl_script), "host-a-gpu", "undefine"], check=False, cwd=config.repo_root, dry_run=dry_run)
    if dry_run:
        log(f"dry-run: would remove {config.state_dir}")
        log(f"dry-run: would remove {config.overlay_path}")
        log(f"dry-run: would remove {config.seed_path}")
        log(f"dry-run: would remove {config.base_image}")
        log(f"dry-run: would remove {config.base_image_sha}")
        log(f"dry-run: would remove {config.base_image_meta}")
        log(f"dry-run: would remove {config.build_gpu_dir}")
    else:
        remove_path(config.state_dir)
        remove_path(config.overlay_path)
        remove_path(config.seed_path)
        remove_path(config.base_image)
        remove_path(config.base_image_sha)
        remove_path(config.base_image_meta)
        remove_path(config.build_gpu_dir)
    for step in (
        ["image", "build", "--variant", "gpu"],
        ["image", "verify", "--variant", "gpu"],
        ["host-a-gpu", "preflight"],
        ["host-a-gpu", "render"],
        ["host-a-gpu", "create-overlay"],
        ["host-a-gpu", "create-seed"],
        ["host-a-gpu", "define"],
        ["host-a-gpu", "start"],
    ):
        run_cmd([str(config.labctl_script), *step], cwd=config.repo_root, dry_run=dry_run)
    return wait_for_guest_ip(config, timeout_s=150, dry_run=dry_run)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def run_gpu_validator(config: HostAConfig, *, dry_run: bool) -> None:
    run_id = f"host-a-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    cmd = [
        sys.executable,
        str(config.gpu_validator_script),
        "validate",
        "--run-id",
        run_id,
        "--vm-name",
        config.domain_name,
        "--inventory",
        str(config.inventory_json),
        "--guest-repo",
        config.guest_repo,
        "--expected-gpu",
        "TITAN RTX",
        "--min-vram-gib",
        "24",
    ]
    run_cmd(cmd, cwd=config.repo_root, dry_run=dry_run)


def http_json(url: str, *, headers: dict[str, str] | None = None) -> object:
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def wait_for_http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: int,
) -> object:
    deadline = time.monotonic() + float(timeout_s)
    last_error = "endpoint not ready"
    while time.monotonic() < deadline:
        try:
            return http_json(url, headers=headers)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(2)
    raise LaneError(f"timed out waiting for {url}: {last_error}")


def wait_for_overlay_clear(controller_host_ip: str, node_id: str, *, timeout_s: int) -> None:
    deadline = time.monotonic() + float(timeout_s)
    url = f"http://{controller_host_ip}:9110/v1/nodes/{node_id}/overlay"
    last_error = "overlay registration not ready"
    while time.monotonic() < deadline:
        try:
            payload = http_json(url, headers={"X-Agent-Token": "devtoken"})
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(2)
            continue
        if isinstance(payload, dict) and payload.get("errors") == []:
            return
        last_error = f"overlay errors present: {payload!r}"
        time.sleep(2)
    raise LaneError(f"timed out waiting for overlay clear: {last_error}")


def do_resume(args: argparse.Namespace) -> int:
    config = load_config(args)
    ensure_lane_dirs(config)
    require_cmd("ssh")
    require_cmd("rsync")
    require_cmd("docker")
    require_cmd("sudo")
    controller_host_ip = resolve_controller_host_ip(args)

    if args.restart_controller:
        run_cmd(["bash", "-lc", "sudo -E make down || true"], cwd=config.repo_root, dry_run=args.dry_run)
        wait_for_controller_shutdown(
            config,
            controller_host_ip,
            timeout_s=args.controller_health_timeout,
            dry_run=args.dry_run,
        )

    start_vm(config, dry_run=args.dry_run)
    guest_ip = args.guest_ip or wait_for_guest_ip(config, timeout_s=args.guest_ip_timeout, dry_run=args.dry_run)
    log(f"guest_ip={guest_ip}")
    log(f"controller_host_ip={controller_host_ip}")

    ensure_nfs_export(config, dry_run=args.dry_run)

    if not args.skip_controller:
        if args.restart_controller or args.dry_run or not controller_healthy():
            start_controller(config, controller_host_ip, dry_run=args.dry_run)
        wait_for_controller_health(
            config,
            controller_host_ip,
            timeout_s=args.controller_health_timeout,
            dry_run=args.dry_run,
        )

    if not args.skip_sync:
        presync_guest_cleanup(config, guest_ip, dry_run=args.dry_run)
        sync_repo(config, guest_ip, dry_run=args.dry_run)

    if not args.skip_node:
        verify_controller_from_guest(config, guest_ip, controller_host_ip, dry_run=args.dry_run)
        wait_for_guest_bootstrap_ready(
            config,
            guest_ip,
            timeout_s=max(int(args.node_ready_timeout), 180),
            dry_run=args.dry_run,
        )
        start_guest_node(config, guest_ip, controller_host_ip, dry_run=args.dry_run)
        wait_for_guest_node(
            config,
            guest_ip,
            controller_host_ip,
            node_timeout_s=args.node_ready_timeout,
            overlay_timeout_s=args.overlay_timeout,
            dry_run=args.dry_run,
        )

    if args.smoke:
        run_smoke(config, guest_ip, dry_run=args.dry_run)

    log("resume complete")
    return 0


def do_rebuild(args: argparse.Namespace) -> int:
    config = load_config(args)
    ensure_lane_dirs(config)
    require_cmd("ssh")
    require_cmd("rsync")
    require_cmd("docker")
    require_cmd("sudo")
    controller_host_ip = resolve_controller_host_ip(args)

    guest_ip = rebuild_guest(config, dry_run=args.dry_run)
    log(f"guest_ip={guest_ip}")
    log(f"controller_host_ip={controller_host_ip}")

    if not args.skip_sync:
        presync_guest_cleanup(config, guest_ip, dry_run=args.dry_run)
        sync_repo(config, guest_ip, dry_run=args.dry_run)

    if not args.skip_gpu_validate:
        run_gpu_validator(config, dry_run=args.dry_run)

    ensure_nfs_export(config, dry_run=args.dry_run)

    if not args.skip_controller:
        start_controller(config, controller_host_ip, dry_run=args.dry_run)
        wait_for_controller_health(
            config,
            controller_host_ip,
            timeout_s=args.controller_health_timeout,
            dry_run=args.dry_run,
        )

    if not args.skip_node:
        verify_controller_from_guest(config, guest_ip, controller_host_ip, dry_run=args.dry_run)
        wait_for_guest_bootstrap_ready(
            config,
            guest_ip,
            timeout_s=max(int(args.node_ready_timeout), 600),
            dry_run=args.dry_run,
        )
        start_guest_node(config, guest_ip, controller_host_ip, dry_run=args.dry_run)
        wait_for_guest_node(
            config,
            guest_ip,
            controller_host_ip,
            node_timeout_s=args.node_ready_timeout,
            overlay_timeout_s=args.overlay_timeout,
            dry_run=args.dry_run,
        )

    if args.smoke:
        run_smoke(config, guest_ip, dry_run=args.dry_run)

    log("rebuild complete")
    return 0


def do_down(args: argparse.Namespace) -> int:
    config = load_config(
        argparse.Namespace(
            env_file=args.env_file,
            guest_key=Path.home() / ".ssh" / "id_rsa",
            guest_port=22,
            apishim_env=DEFAULT_APISHIM_ENV,
            controller_env=DEFAULT_CONTROLLER_ENV,
            server=DEFAULT_APISHIM_SERVER,
        )
    )
    run_cmd(["bash", "-lc", "sudo -E make down || true"], cwd=config.repo_root, dry_run=args.dry_run)
    stop_args = [str(config.labctl_script), "host-a-gpu", "stop"]
    if args.force_vm:
        stop_args.append("--force")
    stop_proc = run_cmd(
        stop_args,
        check=False,
        capture_output=True,
        cwd=config.repo_root,
        dry_run=args.dry_run,
    )
    if not args.dry_run and stop_proc.returncode != 0:
        stop_output = ((stop_proc.stdout or "") + (stop_proc.stderr or "")).lower()
        if "domain is not running" in stop_output:
            log("VM already stopped")
        else:
            raise LaneError(
                ((stop_proc.stdout or "") + (stop_proc.stderr or "")).strip()
                or "failed to stop Host A VM"
            )
    if args.purge_artifacts:
        run_cmd(
            [str(config.labctl_script), "host-a-gpu", "undefine", "--purge-artifacts"],
            cwd=config.repo_root,
            dry_run=args.dry_run,
        )
    log("down complete")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.cmd == "resume":
            return do_resume(args)
        if args.cmd == "rebuild":
            return do_rebuild(args)
        if args.cmd == "down":
            return do_down(args)
        raise LaneError(f"unsupported command: {args.cmd}")
    except LaneError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
