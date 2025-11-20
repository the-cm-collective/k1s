#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/bench/bench_env_prep.sh [--manifest path] [--metrics-port PORT] [--env-file path]

Creates an isolated specs/state directory, starts a controller scoped to the
selected manifest(s), and prints the path to the generated env file.
USAGE
}

manifests=()
metrics_port="9210"
env_file="state/bench-env/env.sh"

# Bench runs do not need ingress writes by default (avoids permission noise).
BENCH_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS:-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      manifests+=("$2"); shift 2;;
    --metrics-port)
      metrics_port="$2"; shift 2;;
    --env-file)
      env_file="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "[bench-env] unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ${#manifests[@]} -eq 0 ]]; then
  manifests+=("specs/examples/echo.yaml")
fi

if [[ $(id -u) -eq 0 ]]; then
  echo "[bench-env] do not run as root; invoke from your user shell" >&2
  exit 3
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
specs_src="$repo_root/specs"
if [[ ! -d "$specs_src" ]]; then
  echo "[bench-env] cannot find specs directory at $specs_src" >&2
  exit 3
fi

export PYTHONPATH="${PYTHONPATH:-$repo_root/src}"

env_dir=$(dirname "$env_file")
spec_dir="$env_dir/specs"
caddy_dir="$env_dir/caddy"
state_db="$env_dir/controller.db"
pid_file="$env_dir/controller.pid"
log_file="$env_dir/controller.log"

# Fresh sandbox each run unless BENCH_REUSE_ENV=1
if [[ -d "$env_dir" && "${BENCH_REUSE_ENV:-0}" != "1" ]]; then
  rm -rf "$env_dir"
fi
mkdir -p "$env_dir"
rm -rf "$spec_dir"
cp -a "$specs_src/." "$spec_dir/"
mkdir -p "$caddy_dir"
rm -f "$state_db"

# Optional cleanup of rootful Podman containers to prevent port conflicts
if command -v sudo >/dev/null 2>&1 && command -v podman >/dev/null 2>&1; then
  mapfile -t rootful_containers < <(sudo podman ps -a --filter label=ae.app --format '{{.ID}}	{{.Names}}	{{.Ports}}' 2>/dev/null | sed '/^$/d')
  if [[ ${#rootful_containers[@]} -gt 0 ]]; then
    echo "[bench-env] detected rootful Podman containers with ae.app label:" >&2
    printf '  %s\n' "${rootful_containers[@]}" >&2
    proceed="${BENCH_AUTOCLEAN_PODMAN:-}"
    if [[ -z "$proceed" ]]; then
      if [[ -t 0 ]]; then
        read -r -p "[bench-env] Remove these containers before benchmarking? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
          proceed="1"
        else
          proceed="0"
        fi
      else
        echo "[bench-env] Set BENCH_AUTOCLEAN_PODMAN=1 to auto-remove or BENCH_AUTOCLEAN_PODMAN=0 to ignore." >&2
        exit 6
      fi
    fi
    if [[ "$proceed" == "1" ]]; then
      ids=$(printf '%s\n' "${rootful_containers[@]}" | awk '{print $1}')
      sudo podman rm -f $ids >/dev/null 2>&1 || true
    else
      echo "[bench-env] refusing to continue while conflicting containers exist." >&2
      exit 6
    fi
  fi
fi

python_output=$(python - "$specs_src" "${manifests[@]}" <<'PY'
import sys, yaml, pathlib
src_root = pathlib.Path(sys.argv[1]).resolve()
manifest_paths = sys.argv[2:]
allowed = []
primary_name = ""
for idx, raw in enumerate(manifest_paths):
    path = pathlib.Path(raw).resolve()
    try:
        rel = path.relative_to(src_root)
    except ValueError:
        print(f"ERR::manifest {path} must be under {src_root}")
        sys.exit(4)
    allowed.append(str(rel))
    if idx == 0:
        with open(path, 'r', encoding='utf-8') as fh:
            for doc in yaml.safe_load_all(fh):
                if isinstance(doc, dict) and doc.get('kind') == 'App':
                    primary_name = (doc.get('metadata') or {}).get('name') or ''
                    break
print('|'.join(allowed))
print(primary_name)
PY
) || {
  err=$(printf '%s' "$python_output" | grep '^ERR::' || true)
  if [[ -n "$err" ]]; then
    echo "${err#ERR::}" >&2
  fi
  exit 4
}

mapfile -t parsed <<<"$python_output"
IFS='|' read -r -a allowed_rel <<<"${parsed[0]}"
primary_app_name="${parsed[1]}"
primary_manifest_rel="${allowed_rel[0]}"

python - "$spec_dir" "${allowed_rel[@]}" <<'PY'
import sys, yaml
from pathlib import Path
spec_root = Path(sys.argv[1])
allowed = set(sys.argv[2:])
if not allowed:
    sys.exit(0)
files = list(spec_root.rglob('*.yaml')) + list(spec_root.rglob('*.yml'))
for path in files:
    rel = str(path.relative_to(spec_root))
    if rel in allowed:
        continue
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding='utf-8')))
    except Exception:
        continue
    if any(isinstance(doc, dict) and str(doc.get('kind', '')).lower() == 'app' for doc in docs):
        path.unlink(missing_ok=True)
PY

if [[ -f "$pid_file" ]]; then
  if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$pid_file"
fi

AE_SPECS_DIR="$spec_dir" AE_STATE_DB="$state_db" AE_CADDY_DIR="$caddy_dir" \
AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}" \
AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-podman}" \
AE_OCI_RUNTIME="${AE_OCI_RUNTIME:-}" \
AE_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS}" \
nohup python -m ae.controller --loop --specs "$spec_dir" --watch --metrics-port "$metrics_port" \
  >"$log_file" 2>&1 &
controller_pid=$!
echo "$controller_pid" > "$pid_file"

for _ in {1..30}; do
  if ! kill -0 "$controller_pid" 2>/dev/null; then
    tail -n 40 "$log_file" >&2
    echo "[bench-env] controller exited early" >&2
    exit 5
  fi
  if grep -q "http api listening" "$log_file" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

mkdir -p "$(dirname "$env_file")"
cat > "$env_file" <<EOF
export AE_SPECS_DIR="$spec_dir"
export AE_STATE_DB="$state_db"
export AE_CADDY_DIR="$caddy_dir"
export AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}"
export AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-podman}"
export AE_OCI_RUNTIME="${AE_OCI_RUNTIME:-}"
export AE_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS}"
export BENCH_ENV_DIR="$env_dir"
export BENCH_CONTROLLER_PID_FILE="$pid_file"
export BENCH_CONTROLLER_PID="$controller_pid"
export BENCH_CONTROLLER_LOG="$log_file"
export BENCH_SPEC_DIR="$spec_dir"
export BENCH_PRIMARY_MANIFEST="$spec_dir/$primary_manifest_rel"
export BENCH_PRIMARY_APP="$primary_app_name"
export BENCH_METRICS_PORT="$metrics_port"
export WAIT_READY_TRIES="${WAIT_READY_TRIES:-60}"
export WAIT_READY_DELAY="${WAIT_READY_DELAY:-2}"
EOF

chmod 600 "$env_file"
printf '%s\n' "$env_file"
