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
capture_timing="warm"
outroot="snapshots"

podman_bin="${AE_PODMAN_BIN:-podman}"
podman_prefix=()
if [[ "${AE_COLLECT_PODMAN_SUDO:-${AE_PODMAN_SUDO:-0}}" == "1" && "$(id -u)" != "0" ]]; then
  # Use a clean sudo env to avoid inheriting rootless Podman vars (e.g. XDG_RUNTIME_DIR).
  podman_prefix=(sudo)
fi

podman_available() {
  command -v "$podman_bin" >/dev/null 2>&1
}

podman_cmd() {
  if [[ ${#podman_prefix[@]} -gt 0 ]]; then
    "${podman_prefix[@]}" "$podman_bin" "$@"
  else
    "$podman_bin" "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="$2"; shift 2;;
    --label) label="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --capture-timing) capture_timing="$2"; shift 2;;
    --outdir) outroot="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

if [[ "$capture_timing" != "warm" && "$capture_timing" != "immediate" ]]; then
  echo "invalid --capture-timing: $capture_timing" >&2
  exit 2
fi

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
  # Prefer explicit engine used for container cgroup collection
  # so metadata reflects where container bytes came from.
  local b
  if [[ -n "${AE_COLLECT_ENGINE:-}" ]]; then
    b="${AE_COLLECT_ENGINE}"
  else
    b="${AE_RUNTIME_BACKEND:-podman}"
  fi
  if [[ "$b" != "podman" && "$b" != "docker" && "$b" != "oci" && "$b" != "cri" ]]; then
    if podman_available; then b=podman; elif command -v docker >/dev/null 2>&1; then b=docker; else b=unknown; fi
  fi
  echo "$b"
}

detect_oci_runtime() {
  local b; b=$(detect_backend)
  local oci=""
  if [[ "$b" == "podman" || "$b" == "oci" ]]; then
    if podman_available; then
      json_info="$(podman_cmd info --format json 2>/dev/null || true)"
      if [[ -n "$json_info" ]]; then
        OCI_JSON="$json_info" oci=$(python - << 'PY'
import json, os, sys
raw = os.environ.get("OCI_JSON", "")
try:
    d = json.loads(raw)
except Exception:
    print(""); sys.exit(0)
h = d.get('host') or d.get('Host') or {}
oci = h.get('ociRuntime') or h.get('OCIRuntime') or {}
name = (oci.get('name') or oci.get('Name') or oci.get('package') or oci.get('path') or '').strip()
name = name.split('/')[-1]
name = name.split()[0] if name else ''
print(name)
PY
        )
      fi
      if [[ -z "$oci" ]]; then
        oci=$(podman_cmd info --format '{{ .Host.OCIRuntime.Name }}' 2>/dev/null | tr -d '"' || true)
      fi
      oci=$(echo "${oci:-}" | tr -d '"' | tr '\n' ' ' | awk '{print $1}')
      if [[ -n "$oci" && ! "$oci" =~ ^[A-Za-z0-9._+-]+$ ]]; then
        oci=""
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
if [[ "${AE_COLLECT_ENGINE:-}" == "podman" || "${AE_COLLECT_ENGINE:-}" == "docker" || "${AE_COLLECT_ENGINE:-}" == "cri" ]]; then
  collect_engine="${AE_COLLECT_ENGINE}"
else
  case "${mode}" in
    k1s)
      if [[ "${AE_RUNTIME_BACKEND:-podman}" == "cri" ]]; then
        collect_engine="cri"
      else
        collect_engine="podman"
      fi
      ;;
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
if [[ "$collect_engine" == "docker" ]] && podman_available; then
  foreign_ae_containers="$(
  (podman_cmd ps -a --format json 2>/dev/null || echo "[]") | python -c '
import json, sys
try:
    arr = json.load(sys.stdin)
except Exception:
    print(0); sys.exit(0)
c=0
for x in arr or []:
    labs=(x.get("Config") or {}).get("Labels") or (x.get("Labels") or {})
    name=(x.get("Name") or "").strip("/ ")
    if labs.get("ae.app") or name.startswith("ae-"):
        c+=1
print(c)
'
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
  echo "  \"capture_timing\": \"${capture_timing}\"," 
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

capture_process_and_container_state() {
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
    # Include cg_path for downstream de-duplication in aggregation
    echo "container_id,name,pid,mem_current_bytes,cg_path"
  } > "${outdir}/raw/containers_mem.csv"

  # CRI (only when selected)
  if [[ "$collect_engine" == "cri" ]]; then
    crictl_bin="${AE_CRICTL_BIN:-crictl}"
    if command -v "$crictl_bin" >/dev/null 2>&1; then
      cri_ps_json="${outdir}/raw/cri_ps.json"
      cri_ps_stderr="${outdir}/raw/cri_ps.stderr"
      cri_pods_json="${outdir}/raw/cri_pods.json"
      cri_pods_stderr="${outdir}/raw/cri_pods.stderr"
      cri_info_json="${outdir}/raw/cri_info.json"
      cri_info_stderr="${outdir}/raw/cri_info.stderr"
      cri_cmd=("$crictl_bin")
      if [[ -n "${AE_CRI_ENDPOINT:-}" ]]; then
        cri_cmd+=("--runtime-endpoint" "${AE_CRI_ENDPOINT}")
      fi
      "${cri_cmd[@]}" info -o json >"${cri_info_json}" 2>"${cri_info_stderr}" || true
      "${cri_cmd[@]}" ps -a -o json >"${cri_ps_json}" 2>"${cri_ps_stderr}" || true
      "${cri_cmd[@]}" pods -o json >"${cri_pods_json}" 2>"${cri_pods_stderr}" || true
      python - "$cri_ps_json" "$cri_pods_json" << 'PY' >> "${outdir}/status.log" 2>>"${outdir}/status.log" || true
import json, sys

def count(path: str, keys: tuple[str, ...]) -> int:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return -1
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return -1

print(f"CRI debug ps_count={count(sys.argv[1], ('containers', 'items'))} pods_count={count(sys.argv[2], ('items',))}")
PY
      python - "$outdir" "$crictl_bin" "${AE_CRI_ENDPOINT:-}" "${cri_ps_json}" << 'PY' 2>>"${outdir}/status.log" >> "${outdir}/raw/containers_mem.csv" || true
import json, os, subprocess, sys
from typing import Optional

root = sys.argv[1]
crictl = sys.argv[2]
endpoint = sys.argv[3] if len(sys.argv) > 3 else ""
ps_json_path = sys.argv[4] if len(sys.argv) > 4 else ""

def run(args: list[str]) -> str:
    cmd = [crictl]
    if endpoint:
        cmd += ["--runtime-endpoint", endpoint]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout

def detect_cgv2() -> bool:
    return os.path.exists("/sys/fs/cgroup/cgroup.controllers")

def normalize_cg(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("/") else "/" + path

def cgroup_path_for_pid(pid: str, want: str = "memory") -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cgroup", "r") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        if not lines:
            return None
        if detect_cgv2():
            for ln in lines:
                parts = ln.split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    return normalize_cg(parts[2])
            last = lines[-1].split(":", 2)[-1]
            return normalize_cg(last)
        for ln in lines:
            parts = ln.split(":", 2)
            if len(parts) == 3 and want in (parts[1] or "").split(","):
                return normalize_cg(parts[2])
        return None
    except Exception:
        return None

def read_mem_for_cg(cg: str) -> int:
    try:
        if not cg:
            return -1
        if detect_cgv2():
            mc = f"/sys/fs/cgroup{cg}/memory.current"
        else:
            mc = f"/sys/fs/cgroup/memory{cg}/memory.usage_in_bytes"
        return int(open(mc, "r").read().strip()) if os.path.exists(mc) else -1
    except Exception:
        return -1

def read_mem_bytes_and_path(pid: str, cg_path: str = ""):
    cg = cgroup_path_for_pid(pid, "memory") if pid and pid != "0" else None
    if cg:
        return read_mem_for_cg(cg), cg
    if cg_path:
        cg = normalize_cg(cg_path)
        return read_mem_for_cg(cg), cg
    return -1, ""

ps_data = {}
if ps_json_path:
    try:
        with open(ps_json_path, "r", encoding="utf-8") as fh:
            ps_data = json.load(fh)
    except Exception:
        ps_data = {}
if not ps_data:
    ps_raw = run(["ps", "-a", "-o", "json"])
    try:
        ps_data = json.loads(ps_raw or "{}")
    except Exception:
        ps_data = {}

containers = ps_data.get("containers") or ps_data.get("items") or []
inspect_out = []

for item in containers:
    cid = item.get("id") or item.get("container_id") or ""
    if not cid:
        continue
    ins_raw = run(["inspect", "-o", "json", cid])
    try:
        ins = json.loads(ins_raw or "{}")
    except Exception:
        ins = {}
    status = ins.get("status") or {}
    info = ins.get("info") or {}
    name = ""
    md = status.get("metadata") or item.get("metadata") or {}
    if isinstance(md, dict):
        name = md.get("name") or ""
    if not name:
        name = item.get("name") or cid[:12]
    labels = status.get("labels") or {}
    # Try to grab cgroupsPath from runtimeSpec (containerd info)
    cg_path = ""
    if isinstance(info, dict):
        rt = info.get("runtimeSpec") or {}
        if isinstance(rt, dict):
            cg_path = ((rt.get("linux") or {}).get("cgroupsPath") or "")
        cg_path = cg_path or (info.get("cgroupsPath") or "")
    pid = str(info.get("pid") or status.get("pid") or 0)
    mem, cg = read_mem_bytes_and_path(pid, cg_path)
    print(f"{cid[:12]},{name},{pid},{mem},{cg}")
    inspect_out.append({
        "Id": cid,
        "Name": name,
        "Labels": labels,
        "Config": {"Labels": labels},
    })

with open(os.path.join(root, "raw", "cri_inspect.json"), "w", encoding="utf-8") as fh:
    json.dump(inspect_out, fh)
PY
    else
      echo "[mem-snapshot] crictl not found (engine_filter=cri); container metrics skipped." >&2
    fi
  fi
  log_step "cri containers collected (if selected)"

  # Podman (only when selected)
  if [[ "$collect_engine" == "podman" || "$collect_engine" == "both" ]] && podman_available; then
    podman_cmd ps -a --format json > "${outdir}/raw/podman_ps.json" 2>/dev/null || true
    ids=$(podman_cmd ps -aq 2>/dev/null || true)
    if [[ -n "${ids}" ]]; then
      podman_cmd inspect --format json $ids > "${outdir}/raw/podman_inspect.json" 2>/dev/null || true
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

def read_mem_bytes_and_path(pid: str):
    try:
        cg = cgroup_path_for_pid(pid, 'memory')
        if not cg:
            return -1, ''
        if detect_cgv2():
            mc = f"/sys/fs/cgroup{cg}/memory.current"
        else:
            mc = f"/sys/fs/cgroup/memory{cg}/memory.usage_in_bytes"
        val = int(open(mc, 'r').read().strip()) if os.path.exists(mc) else -1
        return val, cg
    except Exception:
        return -1, ''

for c in data:
    cid = (c.get('Id', '') or '')[:12]
    name = (c.get('Name', '') or '').strip('/ ')
    pid = str(((c.get('State') or {}).get('Pid') or 0))
    mem, cg = read_mem_bytes_and_path(pid) if pid and pid != '0' else (-1, '')
    print(f"{cid},{name},{pid},{mem},{cg}")
PY
  fi
  log_step "podman containers collected (if selected)"

  # Docker (only when selected)
  if [[ "$collect_engine" == "docker" || "$collect_engine" == "both" ]] && command -v docker >/dev/null 2>&1; then
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

def read_mem_bytes_and_path(pid: str):
    try:
        cg = cgroup_path_for_pid(pid, 'memory')
        if not cg:
            return -1, ''
        if detect_cgv2():
            mc = f"/sys/fs/cgroup{cg}/memory.current"
        else:
            mc = f"/sys/fs/cgroup/memory{cg}/memory.usage_in_bytes"
        val = int(open(mc, 'r').read().strip()) if os.path.exists(mc) else -1
        return val, cg
    except Exception:
        return -1, ''

for c in data:
    cid = c.get('Id','')[:12]
    name = (c.get('Name','') or '').strip('/ ')
    pid = str(((c.get('State') or {}).get('Pid') or 0))
    mem, cg = read_mem_bytes_and_path(pid) if pid and pid!='0' else (-1, '')
    print(f"{cid},{name},{pid},{mem},{cg}")
PY
    fi
  fi
  log_step "docker containers collected (if selected)"

  # k1nd extras: collect control-plane PSS from inside dev containers (when running in docker)
  if [[ "$mode" == "k1s" ]] && command -v docker >/dev/null 2>&1; then
    {
    k1nd_controller_name="${AE_K1ND_CONTROLLER_CONTAINER:-dev-controller-1}"
    k1nd_apishim_name="${AE_K1ND_APISHIM_CONTAINER:-dev-apishim-1}"
    k1nd_ingress_name="${AE_K1ND_INGRESS_CONTAINER:-dev-caddy-1}"
    k1nd_project="${AE_K1ND_PROJECT:-}"

    find_k1nd_container() {
      local name="$1"
      local service="$2"
      local cid=""
      if [[ -n "$name" ]]; then
        cid=$(docker ps -q --filter "name=^${name}$" 2>/dev/null | head -n1 || true)
      fi
      if [[ -z "$cid" && -n "$service" ]]; then
        if [[ -n "$k1nd_project" ]]; then
          cid=$(docker ps -q --filter "label=com.docker.compose.service=${service}" \
            --filter "label=com.docker.compose.project=${k1nd_project}" 2>/dev/null | head -n1 || true)
        else
          cid=$(docker ps -q --filter "label=com.docker.compose.service=${service}" 2>/dev/null | head -n1 || true)
        fi
      fi
      echo "$cid"
    }

    pss_from_container_py() {
      local cid="$1"
      local pattern="$2"
      docker exec -i "$cid" python - "$pattern" << 'PY' 2>/dev/null || true
import os, re, sys
pat = re.compile(sys.argv[1])
total = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode()
    except Exception:
        continue
    if not pat.search(cmd):
        continue
    try:
        with open(f"/proc/{pid}/smaps_rollup", "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
                    break
    except Exception:
        continue
print(total)
PY
    }

    pss_from_container_sh() {
      local cid="$1"
      local comm="$2"
      docker exec "$cid" sh -c '
comm="$1"
total=0
for p in /proc/[0-9]*; do
  pid="${p##*/}"
  [ -r "$p/comm" ] || continue
  name=$(cat "$p/comm" 2>/dev/null || true)
  [ "$name" = "$comm" ] || continue
  if [ -r "$p/smaps_rollup" ]; then
    pss=$(sed -n "s/^Pss:[[:space:]]*\\([0-9]*\\) kB/\\1/p" "$p/smaps_rollup" | head -n1)
    total=$((total + ${pss:-0}))
  fi
done
echo "$total"
' sh "$comm" 2>/dev/null || true
    }

    controller_cid=$(find_k1nd_container "$k1nd_controller_name" "controller")
    apishim_cid=$(find_k1nd_container "$k1nd_apishim_name" "apishim")
    ingress_cid=$(find_k1nd_container "$k1nd_ingress_name" "caddy")

    controller_pss=0
    apishim_pss=0
    ingress_pss=0

    if [[ -n "$controller_cid" ]]; then
      controller_pss=$(pss_from_container_py "$controller_cid" "ae\\.controller") || controller_pss=0
    fi
    if [[ -n "$apishim_cid" ]]; then
      apishim_pss=$(pss_from_container_py "$apishim_cid" "ae\\.apishim") || apishim_pss=0
    fi
    if [[ -n "$ingress_cid" ]]; then
      ingress_pss=$(pss_from_container_sh "$ingress_cid" "caddy") || ingress_pss=0
      if [[ -z "${ingress_pss:-}" || "${ingress_pss:-0}" == "0" ]]; then
        ingress_pss=$(pss_from_container_py "$ingress_cid" "caddy") || ingress_pss=0
      fi
    fi

    total_pss=$(( ${controller_pss:-0} + ${apishim_pss:-0} + ${ingress_pss:-0} ))
    if [[ -n "$controller_cid$apishim_cid$ingress_cid" ]]; then
      cat > "${outdir}/raw/k1nd_control_plane_pss_kb.json" <<EOF
{
  "controller_pss_kb": ${controller_pss:-0},
  "apishim_pss_kb": ${apishim_pss:-0},
  "ingress_pss_kb": ${ingress_pss:-0},
  "total_pss_kb": ${total_pss:-0},
  "containers": {
    "controller": "${controller_cid:-}",
    "apishim": "${apishim_cid:-}",
    "ingress": "${ingress_cid:-}"
  }
}
EOF
      echo "[mem-snapshot] k1nd extras: controller=${controller_pss} apishim=${apishim_pss} ingress=${ingress_pss}" >&2
    fi
    } 2>>"${outdir}/status.log"
  fi

  # Guard rail: fail fast if we expected containers but captured none.
  require_containers="${AE_REQUIRE_CONTAINERS:-}"
  if [[ -z "${require_containers}" ]]; then
    label_lc="${label,,}"
    if [[ "${AE_ALLOW_EMPTY_CONTAINERS:-0}" == "1" ]]; then
      require_containers=0
    elif [[ "${label_lc}" == *"idle"* ]]; then
      require_containers=0
    else
      require_containers=1
    fi
  fi
  if [[ "${require_containers}" == "1" ]]; then
    row_count=0
    if [[ -f "${outdir}/raw/containers_mem.csv" ]]; then
      row_count=$(tail -n +2 "${outdir}/raw/containers_mem.csv" 2>/dev/null | sed '/^$/d' | wc -l | tr -d ' \t')
    fi
    if [[ "${row_count}" == "0" ]]; then
      if [[ "$collect_engine" == "cri" ]]; then
        log_err "no containers captured (AE_REQUIRE_CONTAINERS=1); see raw/cri_ps.json raw/cri_pods.json raw/cri_info.json"
      fi
      log_err "no containers captured (AE_REQUIRE_CONTAINERS=1); failing snapshot"
      exit 4
    fi
  fi

  # k3s extras: collect control-plane PSS and app cgroup bytes from inside k3d node containers
  if [[ "$mode" == "k3s" ]] && command -v docker >/dev/null 2>&1; then
    {
    echo "[mem-snapshot] k3s extras: probing k3d node containers via docker exec" >&2
    k3s_pod_uid_patterns=""
    if [[ -n "${AE_K3S_POD_UIDS:-}" ]]; then
      IFS=',' read -r -a k3s_pod_uids <<< "${AE_K3S_POD_UIDS}"
      for uid in "${k3s_pod_uids[@]}"; do
        uid="${uid// /}"
        [[ -z "$uid" ]] && continue
        if [[ -n "$k3s_pod_uid_patterns" ]]; then
          k3s_pod_uid_patterns+=","
        fi
        k3s_pod_uid_patterns+="${uid}"
        uid_pat="${uid//-/_}"
        if [[ "$uid_pat" != "$uid" ]]; then
          k3s_pod_uid_patterns+=",${uid_pat}"
        fi
      done
    fi
    # Discover k3d server/agent containers
    mapfile -t k3d_nodes < <(docker ps --format '{{.ID}} {{.Names}}' 2>/dev/null | awk '{ if ($2 ~ /k3d-.*-(server|agent)-[0-9]+$/) print $1" "$2 }')
    if (( ${#k3d_nodes[@]} > 0 )); then
      total_cp_pss_kb=0
      total_app_bytes=0
      for entry in "${k3d_nodes[@]}"; do
        cid="${entry%% *}"; cname="${entry#* }"
        # Control-plane PSS: only on server nodes where k3s process exists
        cp_kb=$(docker exec "$cid" sh -c '
# pick a single k3s pid
pid=""
if command -v pidof >/dev/null 2>&1; then
  # choose the lowest PID from pidof output (the main server)
  set -- $(pidof k3s 2>/dev/null || true)
  if [ -n "$1" ]; then pid=$(printf "%s\n" "$@" | sort -n | head -n1); fi
fi
if [ -z "$pid" ]; then
  pid=$(ps -eo pid,comm | awk "$2==\"k3s\"{print $1; exit}")
fi
if [ -n "$pid" ] && [ -r "/proc/$pid/smaps_rollup" ]; then
  sed -n "s/^Pss:\\s*\\([0-9]*\\) kB/\\1/p" "/proc/$pid/smaps_rollup" | head -n1
else
  echo 0
fi' 2>/dev/null || echo 0)
        cp_kb=${cp_kb:-0}
        total_cp_pss_kb=$(( total_cp_pss_kb + ${cp_kb:-0} ))
        # App cgroups: sum memory.current for leaf cgroups matching current app pod UIDs only.
        app_b=$(docker exec -e AE_K3S_POD_UID_PATTERNS="$k3s_pod_uid_patterns" "$cid" sh -c '
if [ -z "${AE_K3S_POD_UID_PATTERNS:-}" ]; then
  echo 0
  exit 0
fi
oldifs="$IFS"
IFS=","
set -- $AE_K3S_POD_UID_PATTERNS
IFS="$oldifs"
for base in /sys/fs/cgroup/kubepods.slice /sys/fs/cgroup/kubepods; do
  if [ -d "$base" ]; then
    find "$base" -type d 2>/dev/null | while read d; do
      [ -f "$d/memory.current" ] || continue
      match=0
      for pat in "$@"; do
        case "$d" in
          *"$pat"*) match=1; break ;;
        esac
      done
      [ "$match" -eq 1 ] || continue
      if find "$d" -mindepth 1 -maxdepth 1 -type d | read _; then
        :
      else
        cat "$d/memory.current"
      fi
    done | awk "{s+=\\$1} END{print s+0}"
    exit 0
  fi
done
echo 0
' 2>/dev/null || echo 0)
        app_b=${app_b:-0}
        total_app_bytes=$(( total_app_bytes + ${app_b:-0} ))
      done
      # Retry app cgroup scan a few times if zero (pods may be starting)
      if [[ "${total_app_bytes}" == "0" ]]; then
        for _ in 1 2 3; do
          sleep 1
          tmp_total=0
          for entry2 in "${k3d_nodes[@]}"; do
            cid2="${entry2%% *}"
            app2=$(docker exec -e AE_K3S_POD_UID_PATTERNS="$k3s_pod_uid_patterns" "$cid2" sh -c '
if [ -z "${AE_K3S_POD_UID_PATTERNS:-}" ]; then
  echo 0
  exit 0
fi
oldifs="$IFS"
IFS=","
set -- $AE_K3S_POD_UID_PATTERNS
IFS="$oldifs"
base=/sys/fs/cgroup/kubepods; [ -d "$base" ] || base=/sys/fs/cgroup/kubepods.slice;
find "$base" -type d 2>/dev/null | while read d; do [ -f "$d/memory.current" ] || continue; match=0; for pat in "$@"; do case "$d" in *"$pat"*) match=1; break ;; esac; done; [ "$match" -eq 1 ] || continue; if find "$d" -mindepth 1 -maxdepth 1 -type d | read _; then :; else cat "$d/memory.current"; fi; done | awk "{s+=\$1} END{print s+0}"
' 2>/dev/null || echo 0)
            tmp_total=$(( tmp_total + ${app2:-0} ))
          done
          if [[ "$tmp_total" != "0" ]]; then
            total_app_bytes=$tmp_total
            break
          fi
        done
      fi
      echo "$total_cp_pss_kb" > "${outdir}/raw/k3s_control_plane_pss_kb.txt" || true
      echo "$total_app_bytes" > "${outdir}/raw/k3s_app_cgroups_bytes.txt" || true
      echo "[mem-snapshot] k3s extras: cp_pss_kb=${total_cp_pss_kb} app_bytes=${total_app_bytes}" >&2
    else
      echo "[mem-snapshot] k3s extras: no k3d node containers detected; skipping inner metrics" >&2
    fi
    } 2>>"${outdir}/status.log"
  fi
}

if [[ "$capture_timing" == "immediate" ]]; then
  capture_process_and_container_state
fi

# Streaming vmstat during warm window (non-fatal)
vmcount=$(( duration > 5 ? duration : 5 ))
vmstat 1 "${vmcount}" > "${outdir}/raw/vmstat.txt" 2>>"${outdir}/status.log" || log_err "vmstat failed"

if [[ "$capture_timing" == "warm" ]]; then
  capture_process_and_container_state
fi

if [[ "$collect_engine" == "podman" ]] && ! podman_available; then
  echo "[mem-snapshot] podman not found (engine_filter=podman); container metrics skipped." >&2
elif [[ "$collect_engine" == "docker" ]] && ! command -v docker >/dev/null 2>&1; then
  echo "[mem-snapshot] docker not found (engine_filter=docker); container metrics skipped." >&2
elif ! podman_available && ! command -v docker >/dev/null 2>&1; then
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
