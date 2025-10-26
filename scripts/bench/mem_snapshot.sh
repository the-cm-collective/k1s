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

echo "[mem-snapshot] mode=${mode} label=${label} duration=${duration}s -> ${outdir}" >&2

# Metadata
{
  echo "{"
  echo "  \"label\": \"${label}\","
  echo "  \"mode\": \"${mode}\","
  echo "  \"duration_sec\": ${duration},"
  echo "  \"timestamp\": \"${ts}\","
  echo "  \"uname\": \"$(uname -a | sed 's/\"/\\\"/g')\""
  echo "}"
} > "${outdir}/meta.json"

# Quick system stats (before)
free -b > "${outdir}/raw/free_before.txt" || true
ps -eo pid,ppid,comm,rss --sort -rss > "${outdir}/raw/ps_before.txt" || true

# Streaming vmstat during warm window (non-fatal)
vmcount=$(( duration > 5 ? duration : 5 ))
vmstat 1 "${vmcount}" > "${outdir}/raw/vmstat.txt" 2>/dev/null || true

# Process target patterns
case "${mode}" in
  k1s)
    proc_pat='python.*ae.controller|caddy|dockerd|containerd'
    ;;
  k3s)
    proc_pat='k3s|containerd|coredns|traefik'
    ;;
  *)
    proc_pat='.'
    ;;
esac

# Capture smaps_rollup + status for matching processes
grep -E "${proc_pat}" "${outdir}/raw/ps_before.txt" | awk '{print $1" "$3}' | while read -r pid comm; do
  [[ -z "${pid}" ]] && continue
  if [[ -r "/proc/${pid}/smaps_rollup" ]]; then
    cp "/proc/${pid}/smaps_rollup" "${outdir}/raw/smaps_${pid}_${comm//\//_}.txt" || true
  fi
  if [[ -r "/proc/${pid}/status" ]]; then
    cp "/proc/${pid}/status" "${outdir}/raw/status_${pid}_${comm//\//_}.txt" || true
  fi
done

# Docker/containers (optional)
if command -v docker >/dev/null 2>&1; then
  docker ps -a --no-trunc --format '{{.ID}} {{.Names}} {{.Status}}' > "${outdir}/raw/docker_ps.txt" || true
  if docker ps -aq >/dev/null 2>&1; then
    ids=$(docker ps -aq)
    if [[ -n "${ids}" ]]; then
      docker inspect ${ids} > "${outdir}/raw/docker_inspect.json" || true
    fi
  fi
  # Try to capture per-container cgroup memory via the main process PID
  {
    echo "container_id,name,pid,mem_current_bytes"
    if [[ -f "${outdir}/raw/docker_inspect.json" ]]; then
      python - << 'PY' 2>/dev/null || true
import json, os, sys
root = sys.argv[1]
path = os.path.join(root, 'raw', 'docker_inspect.json')
try:
    data = json.load(open(path,'r'))
except Exception:
    sys.exit(0)
def read_mem_current(pid:str)->int:
    try:
        cg = open(f"/proc/{pid}/cgroup","r").read().strip().split(':')[-1]
        if not cg.startswith('/'):
            cg = '/' + cg
        # cgroup v2 path
        mc = f"/sys/fs/cgroup{cg}/memory.current"
        return int(open(mc,'r').read().strip()) if os.path.exists(mc) else -1
    except Exception:
        return -1
for c in data:
    cid = c.get('Id','')[:12]
    name = (c.get('Name','') or '').strip('/ ')
    pid = str(((c.get('State') or {}).get('Pid') or 0))
    mem = read_mem_current(pid) if pid and pid!='0' else -1
    print(f"{cid},{name},{pid},{mem}")
PY
      "${outdir}"
    fi
  } > "${outdir}/raw/containers_mem.csv"
else
  echo "[mem-snapshot] docker not found; container cgroup metrics will be skipped. Install Docker or adjust runtime classification." >&2
fi

# Quick system stats (after)
free -b > "${outdir}/raw/free_after.txt" || true
ps -eo pid,ppid,comm,rss --sort -rss > "${outdir}/raw/ps_after.txt" || true

echo "[mem-snapshot] done -> ${outdir}" >&2

echo "${outdir}"
