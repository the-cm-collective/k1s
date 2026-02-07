#!/usr/bin/env bash
set -euo pipefail

# Orchestrate a small matrix of scenarios for k1s and collect memory snapshots.
# Assumes controller is already running (python -m ae.controller --loop) and Docker available.
#
# Usage:
#   scripts/bench/run_matrix.sh --label-suite baseline --app specs/examples/echo.yaml --replicas 1,5,10
#   LABEL_SUITE=baseline make bench-mem-matrix-k1s

label_suite="baseline"
manifest="specs/examples/echo.yaml"
replicas_csv="1,5,10"
mode="k1s"
duration=30
app_name="echo"  # derived from example; can be overridden via --app-name
use_sudo=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --app) manifest="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --mode) mode="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --sudo) use_sudo=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

info() { echo "[matrix] $*" >&2; }

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2; exit 2
  fi
}

python_bin="${PYTHON_BIN:-python}"
require "$python_bin"
repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
py_path="${PYTHONPATH:-$repo_root/src}"
sudo_env_base=(
  "HOME=/root"
  "XDG_RUNTIME_DIR=/run/user/0"
  "DBUS_SESSION_BUS_ADDRESS="
  "CONTAINER_HOST="
  "PODMAN_HOST="
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
if [[ "${AE_CLI_IN_CONTAINER:-0}" == "1" ]] && command -v docker >/dev/null 2>&1; then
  ae() { docker exec "$AE_CLI_CONTAINER" python -m ae.cli "$@"; }
elif [[ "${BENCH_CONTROLLER_SUDO:-0}" == "1" ]] && command -v sudo >/dev/null 2>&1; then
  ae() {
    sudo env "${sudo_env_cli[@]}" "$python_bin" -m ae.cli "$@";
  }
else
  ae() { "$python_bin" -m ae.cli "$@"; }
fi

# Quick bench profile
if [[ "${AE_BENCH_QUICK:-0}" == "1" ]]; then
  : "${WARM_ENABLED:=0}"; export WARM_ENABLED
  : "${DURATION:=5}"; export DURATION
  : "${WAIT_READY_TRIES:=60}"; export WAIT_READY_TRIES
fi

# Optionally warm endpoints (help readiness converge before snapshot)
warm_endpoints() {
  local app="$1"; local seconds="${WARM_SECONDS:-8}"; local backend="${AE_RUNTIME_BACKEND:-podman}"
  command -v curl >/dev/null 2>&1 || return 0
  if [[ "$backend" == "podman" ]] && command -v podman >/dev/null 2>&1; then
    # Discover host ports for app containers via podman JSON, fallback to 'podman port'
    local eps
    eps=$(python - "$app" <<- 'PY'
import json, subprocess, sys
app=sys.argv[1]
try:
    out=subprocess.run(['podman','ps','-a','--format','json'],capture_output=True,text=True,check=False).stdout
    arr=json.loads(out or '[]')
except Exception:
    arr=[]
eps=[]
for c in arr:
    labs=(c.get('Config') or {}).get('Labels') or {}
    if labs.get('ae.app')!=app: continue
    pmap=(c.get('NetworkSettings') or {}).get('Ports') or {}
    got=False
    for k,binds in (pmap or {}).items():
        if not binds: continue
        hp=(binds[0] or {}).get('HostPort')
        hip=((binds[0] or {}).get('HostIp') or '').strip()
        if hp:
            loop='[::1]' if hip.startswith('[') or hip=='::' else '127.0.0.1'
            eps.append(f"{loop}:{hp}"); got=True; break
    if got: continue
    # fallback
    cid=c.get('Id') or ''
    if cid:
        pr=subprocess.run(['podman','port',cid],capture_output=True,text=True,check=False).stdout
        for ln in (pr or '').splitlines():
            try:
                rhs=ln.partition('->')[2].strip()
                hp=rhs.split(':')[-1].strip()
                if hp.isdigit():
                    loop='[::1]' if rhs.startswith('[') else '127.0.0.1'
                    eps.append(f"{loop}:{hp}"); break
            except Exception:
                pass
print('\n'.join(eps))
PY
)
    if [[ -n "$eps" ]]; then
      info "warming ${app} endpoints for ${seconds}s"
      local deadline=$((SECONDS + seconds))
      while (( SECONDS < deadline )); do
        while IFS= read -r ep; do
          [[ -z "$ep" ]] && continue
          curl -fsS --max-time 1 "http://$ep/healthz" >/dev/null 2>&1 || true
        done <<< "$eps"
        sleep 1
      done
    fi
  fi
}

# Build an automatic label base when none provided explicitly
auto_label() {
  local today="r$(date +%Y%m%d)"
  local backend="${AE_RUNTIME_BACKEND:-podman}"
  # Normalize backend tag
  if [[ "$backend" != "podman" && "$backend" != "docker" && "$backend" != "oci" ]]; then
    if command -v podman >/dev/null 2>&1; then backend=podman; elif command -v docker >/dev/null 2>&1; then backend=docker; else backend=unknown; fi
  fi
  # Detect OCI runtime (crun/runc/other) for additional tagging
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
  echo "[matrix] controller not detected; attempting auto-start..." >&2
  # Respect AE_SPECS_DIR if set so we don't inadvertently reconcile every sample under specs/
  SPECS_DIR="${AE_SPECS_DIR:-specs}"
  LOG_FILE="${TMPDIR:-/tmp}/k1s_ctrl_bench.$(id -un).$$.log"
  rm -f "$LOG_FILE" 2>/dev/null || true
  PYTHONPATH="${py_path}" nohup "$python_bin" -m ae.controller --loop --specs "$SPECS_DIR" --metrics-port 9108 --watch >"$LOG_FILE" 2>&1 &
  sleep 3
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    echo "[matrix] controller started (logs: $LOG_FILE)" >&2
    return 0
  fi
  echo "[matrix] controller still not running. Start it manually: 'python -m ae.controller --loop'" >&2
  exit 2
}

preflight_runtime() {
  backend=${AE_RUNTIME_BACKEND:-podman}
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
    if [[ "$use_sudo" == "1" ]]; then
      if ! "$repo_root/scripts/bench/podman_rootful_socket.sh"; then
        echo "[matrix] rootful Podman socket not available (expected /run/podman/podman.sock)." >&2
        exit 2
      fi
    fi
    if ! command -v podman >/dev/null 2>&1; then
      echo "[matrix] Podman not found. Set AE_RUNTIME_BACKEND=docker or install Podman." >&2
      exit 2
    fi
    if ! podman info >/dev/null 2>&1; then
      echo "[matrix] Podman is not ready (podman info failed)." >&2
      echo "        Hints: systemctl --user start podman.socket; loginctl enable-linger $USER; podman system migrate" >&2
      exit 2
    fi
  elif [[ "$backend" == "docker" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
      echo "[matrix] Docker not found. Install Docker or set AE_RUNTIME_BACKEND=podman." >&2
      exit 2
    fi
    if ! docker ps >/dev/null 2>&1; then
      echo "[matrix] Docker not accessible to current user. Ensure group membership or rootless config." >&2
      exit 2
    fi
  fi
}

secrets_guard() {
  if [[ "${AE_ALLOW_PLAINTEXT_SECRETS:-0}" != "1" ]]; then
    if ! command -v sops >/dev/null 2>&1; then
      echo "[matrix] Secrets guard: set AE_ALLOW_PLAINTEXT_SECRETS=1 or install/configure sops for demo secrets." >&2
      exit 2
    fi
  fi
}

if [[ "${SKIP_GUARDS:-0}" != "1" ]]; then
  ensure_controller
  preflight_runtime
  secrets_guard
fi

# If user kept default label 'baseline', switch to an auto label
if [[ "$label_suite" == "baseline" ]]; then
  label_suite="$(auto_label)"
  info "using auto label suite: ${label_suite}"
fi

# Warn if Docker missing (container metrics best-effort)
if ! command -v docker >/dev/null 2>&1; then
  echo "[matrix] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local name="$1"; local want="$2"
  local default_tries=60
  if [[ "$label_suite" =~ ^r[0-9]{8} ]]; then
    default_tries=180
  fi
  local tries=${WAIT_READY_TRIES:-$default_tries}
  local delay=${WAIT_READY_DELAY:-2}
  info "[matrix] wait_ready name=$name target=$want tries=$tries delay=${delay}s"
  while (( tries-- > 0 )); do
    local js
    if ! js=$(ae status "$name" --json 2>/dev/null); then sleep 2; continue; fi
    local ready desired
    ready=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("ready_replicas",0))') || ready=0
    desired=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("desired_replicas",0))') || desired=0
    if [[ "$ready" == "$want" && "$desired" == "$want" ]]; then return 0; fi
    sleep "$delay"
  done
  echo "timeout waiting for $name ready=$want" >&2
  return 1
}

# Idle snapshot (skippable)
if [[ "${SKIP_IDLE:-0}" != "1" ]]; then
  info "idle snapshot"
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_snapshot[@]}" scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration"
    else
      sudo env "${sudo_env_snapshot[@]}" scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration" || true
    fi
  else
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration"
    else
      scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration" || true
    fi
  fi
fi

# Ensure app applied
info "apply manifest: $manifest"
ae apply -f "$manifest" || true

IFS=',' read -r -a reps <<< "$replicas_csv"
for n in "${reps[@]}"; do
  n=${n// /}
  [[ -z "$n" ]] && continue
  info "scale $app_name to $n"
  ae scale "$app_name" --replicas "$n" || true
  wait_ready "$app_name" "$n" || true
  # Optional: prune older revisions to avoid multi-revision accumulation in snapshots
  if [[ "${PRUNE_OLD:-0}" == "1" ]]; then
    if command -v ./scripts/bench/prune_old_revisions.sh >/dev/null 2>&1; then
      ./scripts/bench/prune_old_revisions.sh "$app_name" || true
    fi
  fi
  # Optional warm phase to tickle endpoints before snapshot
  if [[ "${WARM_ENABLED:-1}" == "1" ]]; then
    warm_endpoints "$app_name" || true
  fi
  # Skip if snapshot for this label already exists and SKIP_EXISTING=1
  if [[ "${SKIP_EXISTING:-0}" == "1" ]] && ls -1 "snapshots/${label_suite}-pods-${n}"/* >/dev/null 2>&1; then
    info "skip existing snapshot label=${label_suite}-pods-${n}"
    continue
  fi
  info "snapshot label=${label_suite}-pods-${n}"
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-pods-${n}" --duration "$duration"
    else
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-pods-${n}" --duration "$duration" || true
    fi
  else
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-pods-${n}" --duration "$duration"
    else
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-pods-${n}" --duration "$duration" || true
    fi
  fi
done

info "done"
