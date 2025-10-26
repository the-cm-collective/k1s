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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) label="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --no-sudo) use_sudo=0; shift;;
    --keep-fixtures) stop_fixtures=0; shift;;
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
    snap_dir=$(sudo -E "${cmd[@]}")
  else
    echo "[idle] sudo may prompt for password..." >&2
    snap_dir=$(sudo -E "${cmd[@]}")
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
    while IFS=, read -r cid name pid mem; do
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
