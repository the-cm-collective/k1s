#!/usr/bin/env bash
set -euo pipefail

# Establish an IDLE baseline for k1s with control-plane PSS when possible.
# - Scales all k1s apps to 0 replicas
# - Optionally stops dev fixtures (Prometheus/Caddy) for a pure control-plane view
# - Runs a privileged memory snapshot (sudo) to collect smaps_rollup PSS
# - Aggregates and prints a concise report to stdout and writes summary.txt
#
# Usage:
#   scripts/bench/idle_baseline.sh [--label idle-baseline] [--duration 30] [--no-sudo] [--keep-fixtures]
# Env:
#   SKIP_GUARDS=1 to bypass controller detection

label="idle-baseline"
duration=30
use_sudo=1
stop_fixtures=1
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
    --keep-fixtures) stop_fixtures=0; shift;;
    --restart-docker) export IDLE_RESTART_DOCKER=1; shift;;
    --prune-system) export IDLE_PRUNE_SYSTEM=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

ae() { python -m ae.cli "$@"; }

ensure_controller() {
  if pgrep -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1; then
    return 0
  fi
  echo "[idle] controller not detected. Start it in another terminal: 'python -m ae.controller --loop'" >&2
  exit 2
}

if [[ "${SKIP_GUARDS:-0}" != "1" ]]; then
  ensure_controller
fi

if ! command -v python >/dev/null 2>&1; then
  echo "[idle] python not found" >&2; exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[idle] docker not found; container cgroup metrics will be partial." >&2
fi

# 1) Scale all apps to zero
echo "[idle] scaling all apps to replicas=0" >&2
apps_json=$(ae status --json 2>/dev/null || echo "[]")
app_names=$(echo "$apps_json" | python - << 'PY'
import sys, json
try:
    items=json.load(sys.stdin)
except Exception:
    items=[]
names=[(i.get('app_name') or i.get('name')) for i in items if isinstance(items, list)]
print("\n".join([n for n in names if n]))
PY
)
for name in $app_names; do
  echo "[idle] scale $name -> 0" >&2
  ae scale "$name" --replicas 0 || true
done

# Wait for all to drop to 0 desired
echo "[idle] waiting for apps to reach desired=0" >&2
tries=60
while (( tries-- > 0 )); do
  js=$(ae status --json 2>/dev/null || echo "[]")
  todo=$(echo "$js" | python - << 'PY'
import sys, json
try:
    items=json.load(sys.stdin)
except Exception:
    items=[]
left=[i for i in items if (i.get('desired_replicas') or 0)!=0]
print(len(left))
PY
)
  if [[ "$todo" == "0" ]]; then break; fi
  sleep 2
done

# 2) Optionally stop fixtures for a pure control-plane baseline
if (( stop_fixtures )); then
  if [[ -f ops/dev/docker-compose.yaml ]] && command -v docker >/dev/null 2>&1; then
    echo "[idle] stopping dev fixtures via docker compose" >&2
    docker compose -f ops/dev/docker-compose.yaml down >/dev/null 2>&1 || true
  fi
fi

# Optional: restart docker to reduce daemon memory (stabilize baseline)
if [[ "${IDLE_RESTART_DOCKER:-0}" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  echo "[idle] restarting docker service" >&2
  sudo systemctl restart docker 2>/dev/null || true
  sleep 2
fi

# Optional: prune images/containers/volumes to drop caches before baseline
if [[ "${IDLE_PRUNE_SYSTEM:-0}" == "1" ]] && command -v docker >/dev/null 2>&1; then
  echo "[idle] pruning docker system caches (images/containers/volumes)" >&2
  docker system prune -af --volumes >/dev/null 2>&1 || true
fi

# Best-effort: ensure no demo app containers remain (ae-*)
if command -v docker >/dev/null 2>&1; then
  echo "[idle] pruning lingering app containers (ae-*, ae.app label)" >&2
  ids_a=$(docker ps -aq --filter 'name=^ae-') || ids_a=""
  ids_b=$(docker ps -aq --filter 'label=ae.app') || ids_b=""
  ids=$(printf "%s\n%s\n" "$ids_a" "$ids_b" | sort -u | tr '\n' ' ')
  if [[ -n "${ids// /}" ]]; then
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
  # Wait until no ae.app containers remain
  echo "[idle] waiting for ae.app containers to vanish" >&2
  tries=60
  while (( tries-- > 0 )); do
    left=$(docker ps -aq --filter 'label=ae.app' | wc -l | awk '{print $1}')
    if [[ "$left" == "0" ]]; then break; fi
    sleep 1
  done
fi

# Best-effort: stop local docs server on :9109 if running
if command -v lsof >/dev/null 2>&1; then
  pid=$(lsof -t -i :9109 2>/dev/null | head -1 || true)
  if [[ -n "$pid" ]]; then
    echo "[idle] stopping docs server (pid=$pid on :9109)" >&2
    kill "$pid" 2>/dev/null || true
  fi
else
  if command -v ss >/dev/null 2>&1; then
    pid=$(ss -tulpn 2>/dev/null | awk '/:9109/{print $NF}' | sed 's/.*pid=\([0-9]*\).*/\1/' | head -1)
    if [[ -n "$pid" ]]; then
      echo "[idle] stopping docs server (pid=$pid on :9109)" >&2
      kill "$pid" 2>/dev/null || true
    fi
  fi
fi

# 3) Snapshot with sudo when available (for smaps_rollup PSS)
echo "[idle] taking privileged snapshot (sudo=${use_sudo})" >&2
# Ensure snapshots dirs are writable by current user (may be root-owned from prior runs)
for d in snapshots "snapshots/${label}"; do
  if [[ -d "$d" && ! -w "$d" ]] && command -v sudo >/dev/null 2>&1; then
    sudo chown -R "$(id -u):$(id -g)" "$d" 2>/dev/null || true
  fi
done
cmd=(scripts/bench/mem_snapshot.sh --mode k1s --label "$label" --duration "$duration")
if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    snap_dir=$(sudo env "${sudo_env_snapshot[@]}" "${cmd[@]}")
  else
    echo "[idle] sudo may prompt for password..." >&2
    snap_dir=$(sudo env "${sudo_env_snapshot[@]}" "${cmd[@]}")
  fi
else
  snap_dir=$("${cmd[@]}")
fi

# Ensure ownership so we can aggregate/write summaries without sudo
if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
  sudo chown -R "$(id -u):$(id -g)" "$snap_dir" 2>/dev/null || true
fi

# 4) Aggregate and print (tolerate aggregation failure)
if ! python scripts/bench/mem_aggregate.py "$snap_dir" >/dev/null 2>&1; then
  echo "[idle] WARN: aggregation failed; attempting minimal report from raw files" >&2
fi
summary_json="$snap_dir/summary.json"
if [[ ! -f "$summary_json" ]]; then
  # synthesize a minimal summary.json so reporting still works
  app_b=0; sys_b=0
  if [[ -f "$snap_dir/raw/containers_mem.csv" ]]; then
    # sum mem_current_bytes; heuristic split by name prefix
    while IFS=, read -r cid name pid mem _rest; do
      [[ "$cid" == "container_id" ]] && continue
      [[ -z "$mem" ]] && continue
      [[ "$mem" -lt 0 ]] && continue
      lname="${name,,}"
      if [[ "$lname" == ae-* || "$lname" == *rev* ]]; then
        app_b=$((app_b + mem))
      else
        sys_b=$((sys_b + mem))
      fi
    done < "$snap_dir/raw/containers_mem.csv"
  fi
  cat > "$summary_json" <<EOF
{ "meta": {"label": "${label}", "mode": "k1s", "timestamp": "${snap_dir##*/}"},
  "process_totals_kb": {"pss_kb": 0},
  "overhead": {"pss_kb_control_plane": 0},
  "containers": {"app_mem_bytes": ${app_b}, "system_mem_bytes": ${sys_b}, "total_mem_bytes": $((app_b+sys_b))}
}
EOF
fi

python - "$summary_json" << 'PY' || true
import sys, json, os
p=sys.argv[1]
try:
    data=json.load(open(p,'r'))
except Exception:
    data={'meta':{},'containers':{},'overhead':{},'process_totals_kb':{}}
meta=data.get('meta',{})
cont=data.get('containers',{})
ov=data.get('overhead',{})
def mib(b):
    try: return float(b)/(1024*1024)
    except: return 0.0
report={
  'label': meta.get('label',''),
  'timestamp': meta.get('timestamp',''),
  'mode': meta.get('mode',''),
  'app_mem_mib': round(mib(cont.get('app_mem_bytes',0)),2),
  'system_mem_mib': round(mib(cont.get('system_mem_bytes',0)),2),
  'total_mem_mib': round(mib(cont.get('total_mem_bytes',0)),2),
  'control_plane_pss_mib': round((data.get('process_totals_kb',{}).get('pss_kb',0))/1024.0,2),
  'control_plane_pss_mib_filtered': round((ov.get('pss_kb_control_plane',0))/1024.0,2),
}
txt = []
txt.append(f"Idle baseline ({report['mode']}) @ {report['timestamp']}")
txt.append(f"  Control-plane PSS (MiB): {report['control_plane_pss_mib_filtered']}")
txt.append(f"  System cgroups (MiB):    {report['system_mem_mib']}")
txt.append(f"  App cgroups (MiB):       {report['app_mem_mib']}")
txt.append(f"  Total cgroups (MiB):     {report['total_mem_mib']}")
print("\n".join(txt))
open(os.path.join(os.path.dirname(p),'summary.txt'),'w').write("\n".join(txt)+"\n")
PY

echo "[idle] snapshot: $snap_dir" >&2

# Optional: print per-process PSS breakdown from smaps (top contributors)
python - << 'PY' "$snap_dir" || true
import os, sys, re
snap=sys.argv[1]
raw=os.path.join(snap,'raw')
entries={}
if os.path.isdir(raw):
    for fn in os.listdir(raw):
        if not fn.startswith('smaps_') or not fn.endswith('.txt'): continue
        parts=fn[:-4].split('_',2)
        if len(parts)<3: continue
        pid=parts[1]; comm=parts[2]
        pss=0
        try:
            with open(os.path.join(raw,fn),'r',encoding='utf-8',errors='ignore') as fh:
                for line in fh:
                    if line.startswith('Pss:'):
                        try: pss += int(line.split()[1])
                        except: pass
        except Exception:
            continue
        entries[comm]=entries.get(comm,0)+pss
if entries:
    print('\nPer-process PSS breakdown (MiB):')
    for comm,pss_kb in sorted(entries.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {comm:20s}  {pss_kb/1024.0:.2f}")
PY
