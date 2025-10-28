#!/usr/bin/env bash
set -euo pipefail

# Trigger a rolling update for a k1s app and capture memory snapshots
# during and after the rollout.
#
# Usage:
#   scripts/bench/run_rollout_k1s.sh --label-suite baseline-roll --app specs/examples/echo.yaml --app-name echo --replicas 5 --duration 30

label_suite="baseline-roll"
manifest="specs/examples/echo.yaml"
app_name="echo"
replicas=5
duration=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --app) manifest="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    --replicas) replicas="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
require python

ae() { python -m ae.cli "$@"; }

ensure_controller() {
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    return 0
  fi
  echo "[rollout] controller not detected; attempting auto-start..." >&2
  nohup python -m ae.controller --loop --specs specs/ --metrics-port 9108 --watch >/tmp/k1s_ctrl_bench.log 2>&1 &
  sleep 3
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    echo "[rollout] controller started (logs: /tmp/k1s_ctrl_bench.log)" >&2
    return 0
  fi
  echo "[rollout] controller still not running. Start it manually: 'python -m ae.controller --loop'" >&2
  exit 2
}

preflight_runtime() {
  backend=${AE_RUNTIME_BACKEND:-podman}
  if [[ "$backend" == "podman" || "$backend" == "oci" ]]; then
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

if ! command -v docker >/dev/null 2>&1; then
  echo "[rollout] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local name="$1"; local want="$2"; local tries=120
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

current_image() {
  ae status "$app_name" --json 2>/dev/null | python - << 'PY' || true
import sys, json
try:
    j=json.load(sys.stdin)
    print(j.get('image',''))
except Exception:
    pass
PY
}

switch_image() {
  local in="$1"; local out="$2"; local newimg="$3"
  python - "$in" "$out" "$newimg" << 'PY'
import sys, re
src, dst, newimg = sys.argv[1:4]
data = open(src,'r',encoding='utf-8').read().splitlines()
in_spec=False
changed=False
out=[]
for i,line in enumerate(data):
    if line.strip()=="spec:":
        in_spec=True
        out.append(line); continue
    if in_spec and re.match(r"^\s*image:\s*", line):
        out.append(re.sub(r"^\s*image:\s*.*$", "  image: "+newimg, line))
        in_spec=False; changed=True; continue
    out.append(line)
open(dst,'w',encoding='utf-8').write("\n".join(out)+"\n")
if not changed:
    # fallback: append/replace under spec by inserting after 'spec:'
    pass
PY
}

echo "[rollout] scale ${app_name} to ${replicas} and wait ready" >&2
ae apply -f "$manifest" || true
ae scale "$app_name" --replicas "$replicas" || true
wait_ready "$app_name" "$replicas" || true

base_img=$(current_image)
target_img="$base_img"
if [[ "$base_img" == *demo-blue* ]]; then target_img="demo-green:latest"; fi
if [[ "$base_img" == *demo-green* || -z "$base_img" ]]; then target_img="demo-blue:latest"; fi

tmpman=$(mktemp)
switch_image "$manifest" "$tmpman" "$target_img"

echo "[rollout] apply new image: ${target_img}" >&2
ae apply -f "$tmpman" || true

echo "[rollout] snapshot DURING rollout" >&2
scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true

echo "[rollout] wait ready post-rollout" >&2
wait_ready "$app_name" "$replicas" || true

echo "[rollout] snapshot POST rollout" >&2
scripts/bench/mem_snapshot.sh --mode k1s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true

echo "[rollout] done" >&2
