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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --app) manifest="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --mode) mode="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

info() { echo "[matrix] $*" >&2; }

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2; exit 2
  fi
}

require python

ae() { python -m ae.cli "$@"; }

ensure_controller() {
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    return 0
  fi
  echo "[matrix] controller not detected; attempting auto-start..." >&2
  nohup python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch >/tmp/k1s_ctrl_bench.log 2>&1 &
  sleep 3
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    echo "[matrix] controller started (logs: /tmp/k1s_ctrl_bench.log)" >&2
    return 0
  fi
  echo "[matrix] controller still not running. Start it manually: 'python -m ae.controller --loop'" >&2
  exit 2
}

preflight_runtime() {
  backend=${AE_RUNTIME_BACKEND:-podman}
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
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

# Warn if Docker missing (container metrics best-effort)
if ! command -v docker >/dev/null 2>&1; then
  echo "[matrix] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local name="$1"; local want="$2"; local tries=60
  while (( tries-- > 0 )); do
    local js
    if ! js=$(ae status "$name" --json 2>/dev/null); then sleep 2; continue; fi
    local ready desired
    ready=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("ready_replicas",0))') || ready=0
    desired=$(echo "$js" | python -c 'import sys,json; j=json.load(sys.stdin); print(j.get("desired_replicas",0))') || desired=0
    if [[ "$ready" == "$want" && "$desired" == "$want" ]]; then return 0; fi
    sleep 2
  done
  echo "timeout waiting for $name ready=$want" >&2
  return 1
}

# Idle snapshot
info "idle snapshot"
scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-idle" --duration "$duration" || true

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
  info "snapshot label=${label_suite}-pods-${n}"
  scripts/bench/mem_snapshot.sh --mode "$mode" --label "${label_suite}-pods-${n}" --duration "$duration" || true
done

info "done"
