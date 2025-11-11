#!/usr/bin/env bash
set -euo pipefail

# k1s memory snapshot helper
# - Captures process and cgroup/container memory info into a timestamped folder
# - Designed to be safe and work with or without Docker present
#
# Usage:
#   scripts/bench/mem_snapshot.sh --mode k1s --label idle --duration 30
#   scripts/bench/mem_snapshot.sh --mode k3s --label pods-5 --duration 45

mode="k1s"
label="manual"
duration=30
outroot="snapshots"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="$2"; shift 2;;
    --label) label="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --outdir) outroot="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

ts=$(date +%Y%m%d-%H%M%S)
outdir="${outroot}/${label}/${ts}"
mkdir -p "${outdir}/raw" || true

# Optional verbose tracing for debugging
if [[ "${AE_SNAPSHOT_TRACE:-0}" == "1" ]]; then
  set -x
fi

echo "[mem-snapshot] mode=${mode} label=${label} duration=${duration}s -> ${outdir}" >&2

# Step logger
log_step() { echo "[mem-snapshot] $*" >&2; echo "$(date +%H:%M:%S) $*" >> "${outdir}/status.log"; }
log_err() { echo "[mem-snapshot][error] $*" >&2; echo "$(date +%H:%M:%S) ERROR: $*" >> "${outdir}/status.log"; }
log_step "start: outdir=${outdir}"

# Detect backend and OCI runtime for metadata/labels
detect_backend() {
  local b="${AE_RUNTIME_BACKEND:-podman}"
  if [[ "$b" != "podman" && "$b" != "docker" && "$b" != "oci" ]]; then
    if command -v podman >/dev/null 2>&1; then b=podman; elif command -v docker >/dev/null 2>&1; then b=docker; else b=unknown; fi
  fi
  echo "$b"
}

detect_oci_runtime() {
  local b; b=$(detect_backend)
  local oci=""
  if [[ "$b" == "podman" || "$b" == "oci" ]]; then
    if command -v podman >/dev/null 2>&1; then
      oci=$(podman info --format '{{ .Host.OCIRuntime.Name }}' 2>/dev/null | tr -d '"' || true)
      if [[ -z "$oci" ]]; then
        oci=$(podman info --format json 2>/dev/null | python - << 'PY'
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
  elif [[ "$b" == "docker" ]]; then
    if command -v docker >/dev/null 2>&1; then
      oci=$(docker info --format '{{ .DefaultRuntime }}' 2>/dev/null | tr -d '"' || true)
      if [[ -z "$oci" ]]; then
        oci=$(docker info 2>/dev/null | awk -F': ' '/Default Runtime/ {print $2; exit}')
      fi
    fi
  fi
  echo "$oci"
}

# Decide which engine's containers to collect
collect_engine="both"
if [[ "${AE_COLLECT_ENGINE:-}" == "podman" || "${AE_COLLECT_ENGINE:-}" == "docker" ]]; then
  collect_engine="${AE_COLLECT_ENGINE}"
else
  case "${mode}" in
    k1s) collect_engine="podman";;
    k3s) collect_engine="docker";;
    *) collect_engine="both";;
  esac
fi

# Count foreign-engine ae.app containers to help spot contamination
foreign_ae_containers=0
if [[ "$collect_engine" == "podman" ]] && command -v docker >/dev/null 2>&1; then
  # Count Docker containers with ae.app label or ae-* name via Python to avoid pipefail
  foreign_ae_containers="$(python - << 'PY'
import subprocess, sys
try:
    out = subprocess.run(['docker','ps','-a','--format','{{.Names}} {{.Label "ae.app"}}'], capture_output=True, text=True, check=False).stdout
except Exception:
    print(0); sys.exit(0)
c=0
for ln in (out or '').splitlines():
    parts=ln.strip().split(None,1)
    if not parts: continue
    name=parts[0]
    label=(parts[1] if len(parts)>1 else '').strip()
    if label:
        c+=1
    elif name.startswith('ae-'):
        c+=1
print(c)
PY
)"
fi
if [[ "$collect_engine" == "docker" ]] && command -v podman >/dev/null 2>&1; then
  foreign_ae_containers="$(python - << 'PY'
import json, subprocess, sys
try:
    out = subprocess.run(['podman','ps','-a','--format','json'], capture_output=True, text=True, check=False).stdout
    arr = json.loads(out or '[]')
except Exception:
    print(0); sys.exit(0)
c=0
for x in arr:
    labs=(x.get('Config') or {}).get('Labels') or (x.get('Labels') or {})
    name=(x.get('Name') or '').strip('/ ')
    if labs.get('ae.app') or name.startswith('ae-'):
        c+=1
print(c)
PY
)"
fi
if [[ "$foreign_ae_containers" != "0" ]]; then
  echo "[mem-snapshot] warn: foreign engine has ${foreign_ae_containers} ae.app container(s); excluding them from metrics" >&2
fi

# Optional hard block: refuse to capture when foreign ae.app containers exist
if [[ "${AE_ENGINE_STRICT:-0}" == "1" && "$foreign_ae_containers" != "0" ]]; then
  echo "[mem-snapshot] strict engine mode enabled: refusing to snapshot with ${foreign_ae_containers} foreign ae.app container(s) running" >&2
  echo "[mem-snapshot] hint: stop background demos/labs or unset AE_ENGINE_STRICT=1" >&2
  exit 3
fi

# Metadata
log_step "write meta and preflight"
{
  echo "{"
  echo "  \"label\": \"${label}\"," 
  echo "  \"mode\": \"${mode}\"," 
  echo "  \"duration_sec\": ${duration},"
  echo "  \"timestamp\": \"${ts}\"," 
  echo "  \"uname\": \"$(uname -a | sed 's/\"/\\\"/g')\"," 
  echo "  \"backend\": \"$(detect_backend)\"," 
  echo "  \"oci_runtime\": \"$(detect_oci_runtime)\"," 
  echo "  \"cgroups\": \"$([[ -f /sys/fs/cgroup/cgroup.controllers ]] && echo cg2 || echo cg1)\"," 
  echo "  \"rootless\": $([[ $(id -u) -eq 0 ]] && echo false || echo true),"
  echo "  \"engine_filter\": \"${collect_engine}\","
  echo "  \"foreign_ae_containers\": ${foreign_ae_containers}"
  echo "}"
} > "${outdir}/meta.json"
log_step "meta.json written"

# Quick system stats (before)
free -b > "${outdir}/raw/free_before.txt" 2>>"${outdir}/status.log" || log_err "free_before failed"
# Two views: compact (comm) and scan (includes args)
ps -eo pid,ppid,comm,rss --sort -rss > "${outdir}/raw/ps_before.txt" 2>>"${outdir}/status.log" || log_err "ps_before failed"
ps -eo pid,ppid,comm,args --sort -rss > "${outdir}/raw/ps_scan_before.txt" 2>>"${outdir}/status.log" || log_err "ps_scan_before failed"
log_step "ps snapshots captured"

# Streaming vmstat during warm window (non-fatal)
vmcount=$(( duration > 5 ? duration : 5 ))
vmstat 1 "${vmcount}" > "${outdir}/raw/vmstat.txt" 2>>"${outdir}/status.log" || log_err "vmstat failed"

# Process target patterns
# Use args-aware scan to capture controller accurately and avoid shims
case "${mode}" in
  k1s)
    # match: python -m ae.controller, caddy, docker/containerd, and podman/conmon (but NOT containerd-shim)
    proc_pat='ae\.controller|\bcaddy\b|\bdockerd\b|\bcontainerd\b|\bpodman\b|\bconmon\b'
    scan_file="${outdir}/raw/ps_scan_before.txt"
    ;;
  k3s)
    proc_pat='\bk3s\b|\bcontainerd\b|\bcoredns\b|\btraefik\b'
    scan_file="${outdir}/raw/ps_scan_before.txt"
    ;;
  *)
    proc_pat='.'
    scan_file="${outdir}/raw/ps_scan_before.txt"
    ;;
esac

# Capture smaps_rollup + status for matching processes (exclude containerd-shim)
matches=$(grep -E "${proc_pat}" "${scan_file}" 2>/dev/null | grep -v "containerd-shim" || true)
awk '{print $1" "$3}' <<< "$matches" | while read -r pid comm; do
  [[ -z "${pid}" ]] && continue
  if [[ -r "/proc/${pid}/smaps_rollup" ]]; then
    cp "/proc/${pid}/smaps_rollup" "${outdir}/raw/smaps_${pid}_${comm//\//_}.txt" 2>/dev/null || true
  fi
  if [[ -r "/proc/${pid}/status" ]]; then
    cp "/proc/${pid}/status" "${outdir}/raw/status_${pid}_${comm//\//_}.txt" 2>/dev/null || true
  fi
done
log_step "process smaps/status captured"

## Containers (collect only from the selected engine to avoid contamination)
{
  echo "container_id,name,pid,mem_current_bytes"
} > "${outdir}/raw/containers_mem.csv"

# Podman (only when selected)
if [[ "$collect_engine" != "docker" ]] && command -v podman >/dev/null 2>&1; then
  podman ps -a --format json > "${outdir}/raw/podman_ps.json" 2>/dev/null || true
  ids=$(podman ps -aq 2>/dev/null || true)
  if [[ -n "${ids}" ]]; then
    podman inspect --format json $ids > "${outdir}/raw/podman_inspect.json" 2>/dev/null || true
  fi
  python - "$outdir" << 'PY' 2>>"${outdir}/status.log" >> "${outdir}/raw/containers_mem.csv" || true
import json, os, sys
from typing import Optional

root = sys.argv[1]
inspect_path = os.path.join(root, 'raw', 'podman_inspect.json')
try:
    data = json.load(open(inspect_path, 'r'))
except Exception:
    data = []

def detect_cgv2() -> bool:
    return os.path.exists('/sys/fs/cgroup/cgroup.controllers')

def cgroup_path_for_pid(pid: str, want: str = 'memory') -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cgroup", 'r') as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        if not lines:
            return None
        if detect_cgv2():
            # single unified hierarchy: look for 0::/...
            for ln in lines:
                parts = ln.split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2] if parts[2].startswith('/') else '/' + parts[2]
            # fallback to last field
            last = lines[-1].split(':', 2)[-1]
            return last if last.startswith('/') else '/' + last
        else:
            # cgroup v1: find line where controller list includes 'memory'
            for ln in lines:
                parts = ln.split(':', 2)
                if len(parts) == 3 and want in (parts[1] or '').split(','):
                    p = parts[2]
                    return p if p.startswith('/') else '/' + p
            return None
    except Exception:
        return None

def read_mem_bytes(pid: str) -> int:
    try:
        cg = cgroup_path_for_pid(pid, 'memory')
        if not cg:
            return -1
        if detect_cgv2():
            mc = f"/sys/fs/cgroup{cg}/memory.current"
        else:
            mc = f"/sys/fs/cgroup/memory{cg}/memory.usage_in_bytes"
        return int(open(mc, 'r').read().strip()) if os.path.exists(mc) else -1
    except Exception:
        return -1

for c in data:
    cid = (c.get('Id', '') or '')[:12]
    name = (c.get('Name', '') or '').strip('/ ')
    pid = str(((c.get('State') or {}).get('Pid') or 0))
    mem = read_mem_bytes(pid) if pid and pid != '0' else -1
    print(f"{cid},{name},{pid},{mem}")
PY
fi
log_step "podman containers collected (if selected)"

# Docker (only when selected)
if [[ "$collect_engine" != "podman" ]] && command -v docker >/dev/null 2>&1; then
  docker ps -a --no-trunc --format '{{.ID}} {{.Names}} {{.Status}}' > "${outdir}/raw/docker_ps.txt" || true
  if docker ps -aq >/dev/null 2>&1; then
    ids=$(docker ps -aq)
    if [[ -n "${ids}" ]]; then
      docker inspect ${ids} > "${outdir}/raw/docker_inspect.json" || true
    fi
  fi
  # Try to capture per-container cgroup memory via the main process PID
  if [[ -f "${outdir}/raw/docker_inspect.json" ]]; then
    python - "$outdir" << 'PY' 2>>"${outdir}/status.log" >> "${outdir}/raw/containers_mem.csv" || true
import json, os, sys
from typing import Optional

root = sys.argv[1]
path = os.path.join(root, 'raw', 'docker_inspect.json')
try:
    data = json.load(open(path,'r'))
except Exception:
    sys.exit(0)

def detect_cgv2() -> bool:
    return os.path.exists('/sys/fs/cgroup/cgroup.controllers')

def cgroup_path_for_pid(pid: str, want: str = 'memory') -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cgroup", 'r') as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        if not lines:
            return None
        if detect_cgv2():
            for ln in lines:
                parts = ln.split(':', 2)
                if len(parts) == 3 and parts[0] == '0':
                    return parts[2] if parts[2].startswith('/') else '/' + parts[2]
            last = lines[-1].split(':', 2)[-1]
            return last if last.startswith('/') else '/' + last
        else:
            for ln in lines:
                parts = ln.split(':', 2)
                if len(parts) == 3 and want in (parts[1] or '').split(','):
                    p = parts[2]
                    return p if p.startswith('/') else '/' + p
            return None
    except Exception:
        return None

def read_mem_bytes(pid: str) -> int:
    try:
        cg = cgroup_path_for_pid(pid, 'memory')
        if not cg:
            return -1
        if detect_cgv2():
            mc = f"/sys/fs/cgroup{cg}/memory.current"
        else:
            mc = f"/sys/fs/cgroup/memory{cg}/memory.usage_in_bytes"
        return int(open(mc, 'r').read().strip()) if os.path.exists(mc) else -1
    except Exception:
        return -1

for c in data:
    cid = c.get('Id','')[:12]
    name = (c.get('Name','') or '').strip('/ ')
    pid = str(((c.get('State') or {}).get('Pid') or 0))
    mem = read_mem_bytes(pid) if pid and pid!='0' else -1
    print(f"{cid},{name},{pid},{mem}")
PY
  fi
fi
log_step "docker containers collected (if selected)"

if [[ "$collect_engine" == "podman" ]] && ! command -v podman >/dev/null 2>&1; then
  echo "[mem-snapshot] podman not found (engine_filter=podman); container metrics skipped." >&2
elif [[ "$collect_engine" == "docker" ]] && ! command -v docker >/dev/null 2>&1; then
  echo "[mem-snapshot] docker not found (engine_filter=docker); container metrics skipped." >&2
elif ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
  echo "[mem-snapshot] docker/podman not found; container cgroup metrics will be skipped." >&2
fi

# Quick system stats (after)
free -b > "${outdir}/raw/free_after.txt" 2>>"${outdir}/status.log" || log_err "free_after failed"
ps -eo pid,ppid,comm,rss --sort -rss > "${outdir}/raw/ps_after.txt" 2>>"${outdir}/status.log" || log_err "ps_after failed"

log_step "collection complete; aggregating"
echo "[mem-snapshot] done -> ${outdir}" >&2

# Auto-aggregate so each snapshot has summary.json for downstream combine/docs
# Non-fatal: if Python is missing or aggregation fails, continue.
if command -v python >/dev/null 2>&1; then
  if ! python scripts/bench/mem_aggregate.py "${outdir}" >/dev/null 2>&1; then
    log_err "aggregation failed"
  else
    log_step "aggregation ok"
  fi
else
  echo "[mem-snapshot] warn: python not found; skipping aggregation" >&2
fi

echo "${outdir}"
