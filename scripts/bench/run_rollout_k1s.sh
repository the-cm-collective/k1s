#!/usr/bin/env bash
set -euo pipefail

# Trigger a rolling update for a k1s app and capture memory snapshots
# during and after the rollout.
#
# Usage:
#   scripts/bench/run_rollout_k1s.sh --label-suite baseline-roll --app specs/examples/echo.yaml --app-name echo --replicas 2,5 --duration 30

label_suite="baseline-roll"
manifest="specs/examples/echo.yaml"
app_name="echo"
replicas_csv="2,5"
duration=30
use_sudo=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --app) manifest="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --sudo) use_sudo=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
python_bin="${PYTHON_BIN:-python}"
require "$python_bin"
repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
py_path="${PYTHONPATH:-$repo_root/src}"
sudo_env_base=(
  "HOME=/root"
  "XDG_CONFIG_HOME=/root/.config"
  "XDG_DATA_HOME=/root/.local/share"
  "XDG_CACHE_HOME=/root/.cache"
  "XDG_RUNTIME_DIR=/run/user/0"
  "DBUS_SESSION_BUS_ADDRESS="
  "CONTAINER_HOST="
  "PODMAN_HOST="
)
sudo_env_clean=(
  "-i"
  "PATH=${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
  "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"
  "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}"
  "NIX_LD=${NIX_LD:-}"
)
sudo_env_snapshot=(
  "${sudo_env_base[@]}"
  "AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}"
  "AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}"
  "AE_CRI_ENDPOINT=${AE_CRI_ENDPOINT:-}"
  "AE_CRI_SANDBOX_IMAGE=${AE_CRI_SANDBOX_IMAGE:-}"
  "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
  "AE_COLLECT_ENGINE=${AE_COLLECT_ENGINE:-}"
  "AE_COLLECT_PODMAN_SUDO=${AE_COLLECT_PODMAN_SUDO:-}"
  "AE_PODMAN_SUDO=${AE_PODMAN_SUDO:-}"
  "AE_ENGINE_STRICT=${AE_ENGINE_STRICT:-0}"
  "AE_SNAPSHOT_TRACE=${AE_SNAPSHOT_TRACE:-0}"
)
sudo_env_cli=(
  "${sudo_env_base[@]}"
  "AE_SPECS_DIR=${AE_SPECS_DIR:-specs}"
  "AE_STATE_DB=${AE_STATE_DB:-state/controller.db}"
  "AE_CADDY_DIR=${AE_CADDY_DIR:-state/caddy}"
  "AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS:-1}"
  "AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}"
  "AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}"
  "AE_CRI_ENDPOINT=${AE_CRI_ENDPOINT:-}"
  "AE_CRI_SANDBOX_IMAGE=${AE_CRI_SANDBOX_IMAGE:-}"
  "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
  "AE_DISABLE_INGRESS=${AE_DISABLE_INGRESS:-}"
  "PYTHONPATH=${py_path}"
)

# Auto-detect k1nd single-container runs (k1nd-server) and set sane defaults.
detect_k1nd_container() {
  local name="${AE_CLI_CONTAINER:-k1nd-server}"
  command -v docker >/dev/null 2>&1 || return 1
  if docker ps -q --filter "name=^${name}$" 2>/dev/null | head -n1 | grep -q '.'; then
    echo "$name"
    return 0
  fi
  return 1
}

k1nd_container=""
if [[ "${AE_K1ND_AUTO:-1}" != "0" ]]; then
  k1nd_container="$(detect_k1nd_container || true)"
fi
if [[ -n "$k1nd_container" ]]; then
  : "${AE_CLI_IN_CONTAINER:=1}"
  : "${AE_CLI_CONTAINER:=$k1nd_container}"
  : "${AE_K1ND_CONTROLLER_CONTAINER:=$k1nd_container}"
  : "${AE_K1ND_APISHIM_CONTAINER:=$k1nd_container}"
  : "${AE_K1ND_INGRESS_CONTAINER:=$k1nd_container}"
  : "${AE_COLLECT_ENGINE:=docker}"
  : "${AE_RUNTIME_BACKEND:=docker}"
fi

# Support running ae CLI inside the controller container for k1nd
AE_CLI_CONTAINER=${AE_CLI_CONTAINER:-dev-controller-1}
IN_CONTAINER=0
if [[ "${AE_CLI_IN_CONTAINER:-0}" == "1" ]] && command -v docker >/dev/null 2>&1; then
  IN_CONTAINER=1
  # Wait until the controller container has ae installed (pip -e completes)
  for i in 1 2 3 4 5; do
    if docker exec "$AE_CLI_CONTAINER" python - <<-'PY' >/dev/null 2>&1; then break; fi
import importlib
import sys
sys.exit(0 if importlib.util.find_spec('ae') else 1)
PY
    sleep 2
  done
  ae() { docker exec "$AE_CLI_CONTAINER" python -m ae.cli "$@"; }
elif [[ "${BENCH_CONTROLLER_SUDO:-0}" == "1" ]] && command -v sudo >/dev/null 2>&1; then
  ae() {
    sudo env "${sudo_env_clean[@]}" "${sudo_env_cli[@]}" "$python_bin" -m ae.cli "$@";
  }
else
  ae() { "$python_bin" -m ae.cli "$@"; }
fi

host_manifest="$manifest"
container_apply_dir="/apply"
host_apply_dir="${K1ND_APPLY_DIR:-state/bench-k1nd-apply}"
if [[ "$IN_CONTAINER" == "1" ]]; then
  if [[ ! -f "$host_manifest" && -n "${K1ND_MANIFEST:-}" && -f "${K1ND_MANIFEST:-}" ]]; then
    host_manifest="$K1ND_MANIFEST"
  fi
  mkdir -p "$host_apply_dir"
fi
if [[ ! -f "$host_manifest" ]]; then
  echo "[rollout] manifest not found on host: $host_manifest" >&2
  exit 2
fi

info() { echo "[rollout] $*" >&2; }

# Resolve demo images for rollout based on backend (podman prefers localhost/* to avoid short-name lookups)
backend="${AE_RUNTIME_BACKEND:-podman}"
if [[ "$backend" != "podman" && "$backend" != "docker" && "$backend" != "oci" ]]; then
  if command -v podman >/dev/null 2>&1; then backend=podman; elif command -v docker >/dev/null 2>&1; then backend=docker; else backend=podman; fi
fi
if [[ -n "${AE_ROLLOUT_IMAGE_BLUE:-}" ]]; then
  rollout_blue_image="${AE_ROLLOUT_IMAGE_BLUE}"
else
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
    rollout_blue_image="localhost/demo-blue:latest"
  else
    rollout_blue_image="demo-blue:latest"
  fi
fi
if [[ -n "${AE_ROLLOUT_IMAGE_GREEN:-}" ]]; then
  rollout_green_image="${AE_ROLLOUT_IMAGE_GREEN}"
else
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
    rollout_green_image="localhost/demo-green:latest"
  else
    rollout_green_image="demo-green:latest"
  fi
fi

# Quick bench profile
if [[ "${AE_BENCH_QUICK:-0}" == "1" ]]; then
  : "${DURATION:=5}"; export DURATION
  : "${WAIT_READY_TRIES:=60}"; export WAIT_READY_TRIES
fi

# Build an automatic label base when none provided explicitly
auto_label() {
  local today="r$(date +%Y%m%d)"
  local backend="${AE_RUNTIME_BACKEND:-podman}"
  if [[ "$backend" != "podman" && "$backend" != "docker" && "$backend" != "oci" ]]; then
    if command -v podman >/dev/null 2>&1; then backend=podman; elif command -v docker >/dev/null 2>&1; then backend=docker; else backend=unknown; fi
  fi
  # Detect OCI runtime (crun/runc/other) and add to label
  local oci=""
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
    if command -v podman >/dev/null 2>&1; then
      oci=$(podman info --format '{{ .Host.OCIRuntime.Name }}' 2>/dev/null | tr -d '"' || true)
      if [[ -z "$oci" ]]; then
      oci=$(podman info --format json 2>/dev/null | python - <<- 'PY'
import json, sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
h = d.get('host') or d.get('Host') or {}
oci = h.get('ociRuntime') or h.get('OCIRuntime') or {}
name = (oci.get('name') or oci.get('Name') or oci.get('package') or oci.get('path') or '').strip()
name = name.split('/')[-1]
name = name.split()[0]
print(name)
PY
        )
      fi
    fi
  elif [[ "$backend" == "docker" ]]; then
    if command -v docker >/dev/null 2>&1; then
      oci=$(docker info --format '{{ .DefaultRuntime }}' 2>/dev/null | tr -d '"' || true)
      if [[ -z "$oci" ]]; then
        oci=$(docker info 2>/dev/null | awk -F': ' '/Default Runtime/ {print $2; exit}')
      fi
    fi
  fi
  local root_tag
  if [[ $(id -u) -eq 0 ]]; then root_tag=priv; else root_tag=rootless; fi
  local cg_tag
  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then cg_tag=cg2; else cg_tag=cg1; fi
  if [[ -n "$oci" ]]; then
    echo "${today}+${backend}+${oci}+${root_tag}+${cg_tag}"
  else
    echo "${today}+${backend}+${root_tag}+${cg_tag}"
  fi
}

ensure_controller() {
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    return 0
  fi
  echo "[rollout] controller not detected; attempting auto-start..." >&2
  SPECS_DIR="${AE_SPECS_DIR:-specs}"
  LOG_FILE="${TMPDIR:-/tmp}/k1s_ctrl_bench.$(id -un).$$.log"
  rm -f "$LOG_FILE" 2>/dev/null || true
  PYTHONPATH="${py_path}" nohup "$python_bin" -m ae.controller --loop --specs "$SPECS_DIR" --metrics-port 9108 --watch >"$LOG_FILE" 2>&1 &
  sleep 3
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    echo "[rollout] controller started (logs: $LOG_FILE)" >&2
    return 0
  fi
  echo "[rollout] controller still not running. Start it manually: 'python -m ae.controller --loop'" >&2
  exit 2
}

preflight_runtime() {
  backend=${AE_RUNTIME_BACKEND:-podman}
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
    if [[ "$use_sudo" == "1" ]]; then
      if ! "$repo_root/scripts/bench/podman_rootful_socket.sh"; then
        echo "[rollout] rootful Podman socket not available (expected /run/podman/podman.sock)." >&2
        exit 2
      fi
    fi
    if ! command -v podman >/dev/null 2>&1; then
      echo "[rollout] Podman not found. Set AE_RUNTIME_BACKEND=docker or install Podman." >&2
      exit 2
    fi
    if ! podman info >/dev/null 2>&1; then
      echo "[rollout] Podman is not ready (podman info failed)." >&2
      echo "         Hints: systemctl --user start podman.socket; loginctl enable-linger $USER; podman system migrate" >&2
      exit 2
    fi
  elif [[ "$backend" == "docker" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
      echo "[rollout] Docker not found. Install Docker or set AE_RUNTIME_BACKEND=podman." >&2
      exit 2
    fi
    if ! docker ps >/dev/null 2>&1; then
      echo "[rollout] Docker not accessible to current user. Ensure group membership or rootless config." >&2
      exit 2
    fi
  fi
}

ensure_demo_images() {
  local blue="$1"
  local green="$2"
  shift 2
  local runner=("$@")
  if [[ ${#runner[@]} -eq 0 ]]; then
    return 0
  fi
  local images
  images="$("${runner[@]}" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)"
  if ! grep -q "^${blue}$" <<<"$images"; then
    info "building ${blue}"
    "${runner[@]}" build -t "${blue}" "${repo_root}/samples/servers/blue" >/dev/null 2>&1 || true
  fi
  if ! grep -q "^${green}$" <<<"$images"; then
    info "building ${green}"
    "${runner[@]}" build -t "${green}" "${repo_root}/samples/servers/green" >/dev/null 2>&1 || true
  fi
}

secrets_guard() {
  if [[ "${AE_ALLOW_PLAINTEXT_SECRETS:-0}" != "1" ]]; then
    if ! command -v sops >/dev/null 2>&1; then
      echo "[rollout] Secrets guard: set AE_ALLOW_PLAINTEXT_SECRETS=1 or install/configure sops for demo secrets." >&2
      exit 2
    fi
  fi
}

if [[ "${SKIP_GUARDS:-0}" != "1" ]]; then
  ensure_controller
  preflight_runtime
  secrets_guard
fi

# Ensure demo rollout images exist for the selected engine (rootless/rootful).
default_blue=""
default_green=""
if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
  default_blue="localhost/demo-blue:latest"
  default_green="localhost/demo-green:latest"
elif [[ "$backend" == "docker" ]]; then
  default_blue="demo-blue:latest"
  default_green="demo-green:latest"
fi
if [[ -n "$default_blue" && -n "$default_green" ]]; then
  if [[ "$rollout_blue_image" == "$default_blue" && "$rollout_green_image" == "$default_green" ]]; then
    if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
      if [[ "$use_sudo" == "1" ]]; then
        ensure_demo_images "$default_blue" "$default_green" sudo podman
      else
        ensure_demo_images "$default_blue" "$default_green" podman
      fi
    elif [[ "$backend" == "docker" ]]; then
      ensure_demo_images "$default_blue" "$default_green" docker
    fi
  fi
fi

# If user kept default label 'baseline-roll', switch to an auto label
if [[ "$label_suite" == "baseline-roll" ]]; then
  label_suite="$(auto_label)"
  echo "[rollout] using auto label suite: ${label_suite}" >&2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[rollout] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local name="$1"; local want="$2"; local tries=${WAIT_READY_TRIES:-120}
  local delay=${WAIT_READY_DELAY:-2}
  local use_runtime_wait="${BENCH_WAIT_RUNTIME:-0}"
  local backend="${AE_RUNTIME_BACKEND:-podman}"
  info "[rollout] wait_ready name=$name target=$want tries=$tries delay=${delay}s"
  while (( tries-- > 0 )); do
    local js status_ok=0 ready desired
    if js=$(ae status "$name" --json 2>/dev/null); then
      ready=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("ready_replicas",0))') || ready=0
      desired=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("desired_replicas",0))') || desired=0
      status_ok=1
    else
      ready=0
      desired=0
    fi
    if [[ "$ready" == "$want" && "$desired" == "$want" ]]; then return 0; fi
    if [[ "$use_runtime_wait" == "1" && "$status_ok" == "0" ]]; then
      local count=0
      if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
        if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
          count=$(sudo env "${sudo_env_base[@]}" "${AE_PODMAN_BIN:-podman}" ps --filter "label=ae.app=${name}" --format "{{.ID}}" 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' \t') || count=0
        else
          count=$("${AE_PODMAN_BIN:-podman}" ps --filter "label=ae.app=${name}" --format "{{.ID}}" 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' \t') || count=0
        fi
      elif [[ "$backend" == "docker" ]]; then
        if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
          count=$(sudo env "${sudo_env_base[@]}" docker ps --filter "label=ae.app=${name}" --format "{{.ID}}" 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' \t') || count=0
        else
          count=$(docker ps --filter "label=ae.app=${name}" --format "{{.ID}}" 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' \t') || count=0
        fi
      fi
      if [[ "$count" -ge "$want" ]]; then return 0; fi
    fi
    sleep "$delay"
  done
  echo "timeout waiting for $name ready=$want" >&2
  return 1
}

settle_current_revision() {
  local name="$1"; local want="$2"
  info "[rollout] settle current revision name=$name target=$want"
  ae scale "$name" --replicas "$want" >/dev/null
  wait_ready "$name" "$want"
}

current_image() {
  ae status "$app_name" --json 2>/dev/null | python - <<- 'PY' || true
import sys, json
try:
    j=json.load(sys.stdin)
    print(j.get('image',''))
except Exception:
    pass
PY
}

switch_image() {
  local in="$1"; local out="$2"; local newimg="$3"; local replicas="$4"
  python - "$in" "$out" "$newimg" "$replicas" <<- 'PY'
import sys, re
src, dst, newimg, replicas = sys.argv[1:5]
try:
    replicas = int(replicas)
except Exception:
    replicas = None
data = open(src,'r',encoding='utf-8').read().splitlines()
out=[]
in_spec=False
did_img=False
did_rep=False
for i,line in enumerate(data):
    if line.strip()=="spec:":
        in_spec=True
        out.append(line)
        # If we won't find explicit replicas below, insert one right after spec:
        # (deferred: only insert after scanning unless found)
        continue
    if in_spec:
        if re.match(r"^\s*image:\s*", line):
            out.append(re.sub(r"^\s*image:\s*.*$", "  image: "+newimg, line))
            did_img=True
            continue
        if replicas is not None and re.match(r"^\s*replicas:\s*", line):
            out.append(re.sub(r"^\s*replicas:\s*.*$", f"  replicas: {replicas}", line))
            did_rep=True
            continue
        # Leave spec block when next top-level key appears
        if re.match(r"^[^\s]", line):
            in_spec=False
    out.append(line)

# If spec existed but replicas wasn't present and we have a value, insert it after 'spec:'
if replicas is not None and not did_rep:
    out2=[]
    inserted=False
    for i,line in enumerate(out):
        out2.append(line)
        if not inserted and line.strip()=="spec:":
            out2.append(f"  replicas: {replicas}")
            inserted=True
    out=out2

open(dst,'w',encoding='utf-8').write("\n".join(out)+"\n")
PY
}

rollout_replicas=()
IFS=',' read -r -a rollout_replicas_raw <<< "$replicas_csv"
for rep in "${rollout_replicas_raw[@]}"; do
  rep="${rep// /}"
  [[ -z "$rep" ]] && continue
  if [[ ! "$rep" =~ ^[0-9]+$ ]]; then
    echo "[rollout] invalid replicas '${rep}' (expected integer); skipping" >&2
    continue
  fi
  rollout_replicas+=("$rep")
done
if (( ${#rollout_replicas[@]} == 0 )); then
  echo "[rollout] no valid replicas provided (got: '${replicas_csv}')" >&2
  exit 2
fi

run_rollout_once() {
  local replicas="$1"

  echo "[rollout] scale ${app_name} to ${replicas} and wait ready" >&2
  # Apply a manifest with replicas set to avoid single-replica host port publishing collisions
  local startman
  local host_startman
  if [[ "$IN_CONTAINER" == "1" ]]; then
    host_startman="${host_apply_dir}/rollout-start-${app_name}-${replicas}.yaml"
    startman="${container_apply_dir}/rollout-start-${app_name}-${replicas}.yaml"
  else
    host_startman=$(mktemp)
    startman="$host_startman"
  fi
  python - "$host_manifest" "$host_startman" "$replicas" <<-'PY'
import sys, re
src, dst, replicas = sys.argv[1:4]
try:
    replicas = int(replicas)
except Exception:
    replicas = None
data = open(src,'r',encoding='utf-8').read().splitlines()
out=[]
in_spec=False
did_rep=False
for i,line in enumerate(data):
    if line.strip()=="spec:":
        in_spec=True
        out.append(line)
        continue
    if in_spec:
        if replicas is not None and re.match(r"^\s*replicas:\s*", line):
            out.append(re.sub(r"^\s*replicas:\s*.*$", f"  replicas: {replicas}", line))
            did_rep=True
            continue
        if re.match(r"^[^\s]", line):
            in_spec=False
    out.append(line)
if replicas is not None and not did_rep:
    out2=[]; inserted=False
    for line in out:
        out2.append(line)
        if not inserted and line.strip()=="spec:":
            out2.append(f"  replicas: {replicas}"); inserted=True
    out=out2
open(dst,'w',encoding='utf-8').write("\n".join(out)+"\n")
PY
  ae apply -f "$startman"
  ae scale "$app_name" --replicas "$replicas"
  wait_ready "$app_name" "$replicas"
  settle_current_revision "$app_name" "$replicas"

  local base_img
  local target_img
  base_img=$(current_image)
  target_img="$base_img"
  if [[ "$base_img" == *demo-blue* ]]; then target_img="$rollout_green_image"; fi
  if [[ "$base_img" == *demo-green* || -z "$base_img" ]]; then target_img="$rollout_blue_image"; fi

  local tmpman
  local host_tmpman
  if [[ "$IN_CONTAINER" == "1" ]]; then
    host_tmpman="${host_apply_dir}/rollout-${app_name}-${replicas}.yaml"
    tmpman="${container_apply_dir}/rollout-${app_name}-${replicas}.yaml"
  else
    host_tmpman=$(mktemp)
    tmpman="$host_tmpman"
  fi
  switch_image "$host_manifest" "$host_tmpman" "$target_img" "$replicas"

  echo "[rollout] apply new image: ${target_img}" >&2
  ae apply -f "$tmpman"

  echo "[rollout] snapshot DURING rollout" >&2
  # Skip if existing and SKIP_EXISTING=1
  if [[ "${SKIP_EXISTING:-0}" == "1" ]] && ls -1 "snapshots/${label_suite}-rollout-${replicas}-during"/* >/dev/null 2>&1; then
    echo "[rollout] skip existing DURING snapshot ${label_suite}-rollout-${replicas}-during" >&2
  else
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_clean[@]}" "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration"
    else
      sudo env "${sudo_env_clean[@]}" "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true
    fi
    else
      if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration"
      else
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true
      fi
    fi
  fi

  echo "[rollout] wait ready post-rollout" >&2
  wait_ready "$app_name" "$replicas"
  settle_current_revision "$app_name" "$replicas"

  echo "[rollout] snapshot POST rollout" >&2
  # Skip if existing and SKIP_EXISTING=1
  if [[ "${SKIP_EXISTING:-0}" == "1" ]] && ls -1 "snapshots/${label_suite}-rollout-${replicas}-post"/* >/dev/null 2>&1; then
    echo "[rollout] skip existing POST snapshot ${label_suite}-rollout-${replicas}-post" >&2
  else
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_clean[@]}" "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration"
    else
      sudo env "${sudo_env_clean[@]}" "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true
    fi
    else
      if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration"
      else
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true
      fi
    fi
  fi
}

for replicas in "${rollout_replicas[@]}"; do
  run_rollout_once "$replicas"
done

echo "[rollout] done" >&2
