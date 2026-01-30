#!/usr/bin/env bash
set -euo pipefail

# Establish an IDLE baseline for k3s (via k3d) with control-plane PSS when possible.

label="idle-k3s"
duration=30
use_sudo=1
ns="default"
sudo_env_snapshot=(
  "HOME=/root"
  "XDG_RUNTIME_DIR=/run/user/0"
  "DBUS_SESSION_BUS_ADDRESS="
  "CONTAINER_HOST="
  "PODMAN_HOST="
  "AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}"
  "AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}"
  "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
  "AE_COLLECT_ENGINE=${AE_COLLECT_ENGINE:-}"
  "AE_COLLECT_PODMAN_SUDO=${AE_COLLECT_PODMAN_SUDO:-}"
  "AE_PODMAN_SUDO=${AE_PODMAN_SUDO:-}"
  "AE_ENGINE_STRICT=${AE_ENGINE_STRICT:-0}"
  "AE_SNAPSHOT_TRACE=${AE_SNAPSHOT_TRACE:-0}"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) label="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --no-sudo) use_sudo=0; shift;;
    --namespace) ns="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
require kubectl

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "[idle-k3s] kubectl cannot reach a cluster. Create one with 'make bench-k3s-up' (k3d required)." >&2
  exit 2
fi

echo "[idle-k3s] scaling all deployments to 0 in namespace=$ns" >&2
deps=$(kubectl -n "$ns" get deploy -o name 2>/dev/null || true)
for d in $deps; do
  kubectl -n "$ns" scale "$d" --replicas 0 >/dev/null 2>&1 || true
done

echo "[idle-k3s] waiting for readyReplicas=0" >&2
tries=60
while (( tries-- > 0 )); do
  ready=$(kubectl -n "$ns" get deploy -o jsonpath='{range .items[*]}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null | grep -v '^$' | grep -v '^0$' | wc -l | awk '{print $1}')
  if [[ "$ready" == "0" ]]; then break; fi
  sleep 2
done

echo "[idle-k3s] taking privileged snapshot (sudo=${use_sudo})" >&2
for d in snapshots "snapshots/${label}"; do
  if [[ -d "$d" && ! -w "$d" ]] && command -v sudo >/dev/null 2>&1; then
    sudo chown -R "$(id -u):$(id -g)" "$d" 2>/dev/null || true
  fi
done
cmd=(scripts/bench/mem_snapshot.sh --mode k3s --label "$label" --duration "$duration")
if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    snap_dir=$(sudo env "${sudo_env_snapshot[@]}" "${cmd[@]}")
  else
    echo "[idle-k3s] sudo may prompt for password..." >&2
    snap_dir=$(sudo env "${sudo_env_snapshot[@]}" "${cmd[@]}")
  fi
else
  snap_dir=$("${cmd[@]}")
fi
if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
  sudo chown -R "$(id -u):$(id -g)" "$snap_dir" 2>/dev/null || true
fi

if ! python scripts/bench/mem_aggregate.py "$snap_dir" >/dev/null 2>&1; then
  echo "[idle-k3s] WARN: aggregation failed" >&2
fi

python - "$snap_dir/summary.json" << 'PY' || true
import sys, json, os
p=sys.argv[1]
try:
    data=json.load(open(p,'r'))
except Exception:
    data={'meta':{},'containers':{},'overhead':{},'process_totals_kb':{},'pss_breakdown_mib':{}}
meta=data.get('meta',{})
cont=data.get('containers',{})
ov=data.get('overhead',{})
bk=data.get('pss_breakdown_mib',{})
def mib(b):
    try: return float(b)/(1024*1024)
    except: return 0.0
print(f"Idle baseline (k3s) @ {meta.get('timestamp','')}")
print(f"  Control-plane PSS (MiB): {bk.get('k3s_control_plane',0.0)}")
print(f"  System cgroups (MiB):    {round(mib(cont.get('system_mem_bytes',0)),2)}")
print(f"  App cgroups (MiB):       {round(mib(cont.get('app_mem_bytes',0)),2)}")
print(f"  Total cgroups (MiB):     {round(mib(cont.get('total_mem_bytes',0)),2)}")
PY

echo "[idle-k3s] snapshot: $snap_dir" >&2
