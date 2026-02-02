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
controller_mode="user"

# Bench runs do not need ingress writes by default (avoids permission noise).
BENCH_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS:-1}"
# Keep specs empty by default so file-based reconcile won't override scale/apply during benches.
: "${BENCH_SPECS_EMPTY:=1}"
# Keep nodes eligible during long bench runs (override via BENCH_NODE_NOTREADY_AFTER or AE_NODE_NOTREADY_AFTER).
bench_node_notready_after="${BENCH_NODE_NOTREADY_AFTER:-${AE_NODE_NOTREADY_AFTER:-600}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      manifests+=("$2"); shift 2;;
    --metrics-port)
      metrics_port="$2"; shift 2;;
    --env-file)
      env_file="$2"; shift 2;;
    --sudo-controller)
      controller_mode="sudo"; shift;;
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
specs_src="${BENCH_SPECS_SRC:-$repo_root/specs}"
if [[ ! -d "$specs_src" ]]; then
  echo "[bench-env] cannot find specs directory at $specs_src" >&2
  exit 3
fi

export PYTHONPATH="${PYTHONPATH:-$repo_root/src}"
python_bin="${PYTHON_BIN:-$(command -v python)}"
if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
  echo "[bench-env] python not found on PATH; set PYTHON_BIN explicitly" >&2
  exit 3
fi
podman_bin="${AE_PODMAN_BIN:-$(command -v podman || true)}"
if [[ -z "$podman_bin" ]]; then
  podman_bin="podman"
fi

check_netavark_isolation() {
  if [[ "$controller_mode" != "sudo" ]]; then
    return 0
  fi
  if ! command -v sudo >/dev/null 2>&1 || ! command -v podman >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v nft >/dev/null 2>&1; then
    return 0
  fi
  local backend
  backend=$(sudo podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null | tr -d '\r')
  if [[ "$backend" != "netavark" ]]; then
    return 0
  fi
  local chain decision
  chain=$(sudo nft -a list chain inet netavark NETAVARK-ISOLATION-3 2>/dev/null || true)
  if [[ -z "$chain" ]]; then
    return 0
  fi
  decision=$(printf '%s\n' "$chain" | awk '
    /oifname "podman0"/ {
      if ($0 ~ /accept/) { print "accept"; exit }
      if ($0 ~ /drop/) { print "drop"; exit }
    }
  ')
  if [[ "$decision" == "drop" ]]; then
    echo "[bench-env] netavark isolation drops podman0; host-port forwarding will fail for rootful Podman." >&2
    echo "[bench-env] Fix (insert before drop): sudo nft insert rule inet netavark NETAVARK-ISOLATION-3 oifname \"podman0\" accept" >&2
    echo "[bench-env] Or temporarily flush: sudo nft flush chain inet netavark NETAVARK-ISOLATION-3" >&2
    exit 6
  fi
}

env_dir=$(dirname "$env_file")
spec_dir="$env_dir/specs"
apply_dir="$env_dir/apply"
caddy_dir="$env_dir/caddy"
state_db="$env_dir/controller.db"
pid_file="$env_dir/controller.pid"
log_file="$env_dir/controller.log"

# Fresh sandbox each run unless BENCH_REUSE_ENV=1
if [[ -d "$env_dir" && "${BENCH_REUSE_ENV:-0}" != "1" ]]; then
  rm -rf "$env_dir"
fi
mkdir -p "$env_dir"
mkdir -p "$caddy_dir"
rm -f "$state_db"

# Pre-create controller log/pid files so sudo controller keeps user ownership.
: > "$log_file"
: > "$pid_file"
chmod 664 "$log_file" "$pid_file" 2>/dev/null || true

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

python_output=$("$python_bin" - "$specs_src" "${manifests[@]}" <<'PY'
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
                if isinstance(doc, dict):
                    kind = str(doc.get('kind', '')).strip().lower()
                    if kind in ('app', 'deployment'):
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
IFS='|' read -r -a allowed_rel <<<"${parsed[0]:-}"
if [[ ${#allowed_rel[@]} -eq 0 || -z "${allowed_rel[0]:-}" ]]; then
  echo "[bench-env] failed to parse manifest list from helper" >&2
  exit 4
fi
primary_app_name="${parsed[1]-}"
primary_manifest_rel="${allowed_rel[0]}"

rm -rf "$spec_dir"
rm -rf "$apply_dir"
primary_manifest_path=""
if [[ "${BENCH_SPECS_EMPTY:-0}" == "1" ]]; then
  # Keep the controller's spec dir empty so file-based reconcile doesn't
  # overwrite bench-driven scale/apply changes.
  mkdir -p "$spec_dir" "$apply_dir"
  for rel in "${allowed_rel[@]}"; do
    src_path="$specs_src/$rel"
    dest_path="$apply_dir/$rel"
    if [[ ! -f "$src_path" ]]; then
      echo "[bench-env] manifest not found at $src_path" >&2
      exit 4
    fi
    mkdir -p "$(dirname "$dest_path")"
    cp -f "$src_path" "$dest_path"
  done
  primary_manifest_path="$apply_dir/$primary_manifest_rel"
else
  if [[ "${BENCH_SPECS_MINIMAL:-0}" == "1" ]]; then
    mkdir -p "$spec_dir"
    for rel in "${allowed_rel[@]}"; do
      src_path="$specs_src/$rel"
      dest_path="$spec_dir/$rel"
      if [[ ! -f "$src_path" ]]; then
        echo "[bench-env] manifest not found at $src_path" >&2
        exit 4
      fi
      mkdir -p "$(dirname "$dest_path")"
      cp -f "$src_path" "$dest_path"
    done
  else
    cp -a "$specs_src/." "$spec_dir/"
    "$python_bin" - "$spec_dir" "${allowed_rel[@]}" <<'PY'
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
    if any(
        isinstance(doc, dict)
        and str(doc.get('kind', '')).strip().lower() in ('app', 'deployment')
        for doc in docs
    ):
        path.unlink(missing_ok=True)
PY
  fi
  primary_manifest_path="$spec_dir/$primary_manifest_rel"
fi

if [[ -f "$pid_file" ]]; then
  if kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$pid_file"
fi

if [[ "$controller_mode" == "sudo" ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[bench-env] sudo not found; cannot start controller with sudo" >&2
    exit 3
  fi
  check_netavark_isolation
  if ! "$repo_root/scripts/bench/podman_rootful_socket.sh"; then
    echo "[bench-env] rootful podman socket not available; aborting" >&2
    exit 5
  fi
  sudo env \
    PYTHON_BIN="$python_bin" \
    AE_PODMAN_BIN="$podman_bin" \
    PYTHONPATH="${PYTHONPATH:-$repo_root/src}" \
    HOME="/root" \
    XDG_RUNTIME_DIR="/run/user/0" \
    DBUS_SESSION_BUS_ADDRESS="" \
    CONTAINER_HOST="" \
    PODMAN_HOST="" \
    AE_SPECS_DIR="$spec_dir" \
    AE_STATE_DB="$state_db" \
    AE_CADDY_DIR="$caddy_dir" \
    AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}" \
    AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-podman}" \
    AE_OCI_RUNTIME="${AE_OCI_RUNTIME:-}" \
    AE_CRI_ENDPOINT="${AE_CRI_ENDPOINT:-}" \
    AE_CRI_SANDBOX_IMAGE="${AE_CRI_SANDBOX_IMAGE:-}" \
    AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-}" \
    AE_NODE_NOTREADY_AFTER="${bench_node_notready_after}" \
    AE_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS}" \
    BENCH_METRICS_PORT="$metrics_port" \
    BENCH_LOG_FILE="$log_file" \
    BENCH_PID_FILE="$pid_file" \
    bash -lc 'install -d -m 0700 /run/user/0 || true; nohup "$PYTHON_BIN" -m ae.controller --loop --specs "$AE_SPECS_DIR" --watch --metrics-port "$BENCH_METRICS_PORT" >>"$BENCH_LOG_FILE" 2>&1 & echo $! > "$BENCH_PID_FILE"' </dev/null
  controller_pid=$(cat "$pid_file" 2>/dev/null || true)
else
  AE_SPECS_DIR="$spec_dir" AE_STATE_DB="$state_db" AE_CADDY_DIR="$caddy_dir" \
  AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}" \
  AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-podman}" \
  AE_OCI_RUNTIME="${AE_OCI_RUNTIME:-}" \
  AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-}" \
  AE_NODE_NOTREADY_AFTER="${bench_node_notready_after}" \
  AE_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS}" \
  nohup "$python_bin" -m ae.controller --loop --specs "$spec_dir" --watch --metrics-port "$metrics_port" \
    >>"$log_file" 2>&1 &
  controller_pid=$!
  echo "$controller_pid" > "$pid_file"
fi

for _ in {1..30}; do
  if [[ "$controller_mode" == "sudo" ]]; then
    if ! sudo kill -0 "$controller_pid" 2>/dev/null; then
      tail -n 40 "$log_file" >&2
      echo "[bench-env] controller exited early" >&2
      exit 5
    fi
  else
    if ! kill -0 "$controller_pid" 2>/dev/null; then
      tail -n 40 "$log_file" >&2
      echo "[bench-env] controller exited early" >&2
      exit 5
    fi
  fi
  if grep -q "http api listening" "$log_file" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! grep -q "http api listening" "$log_file" >/dev/null 2>&1; then
  tail -n 40 "$log_file" >&2
  echo "[bench-env] controller exited early" >&2
  exit 5
fi

mkdir -p "$(dirname "$env_file")"
cat > "$env_file" <<EOF
export AE_SPECS_DIR="$spec_dir"
export AE_STATE_DB="$state_db"
export AE_CADDY_DIR="$caddy_dir"
export AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}"
export AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-podman}"
export AE_OCI_RUNTIME="${AE_OCI_RUNTIME:-}"
export AE_CRI_ENDPOINT="${AE_CRI_ENDPOINT:-}"
export AE_CRI_SANDBOX_IMAGE="${AE_CRI_SANDBOX_IMAGE:-}"
export AE_PODMAN_BIN="$podman_bin"
export AE_DISABLE_INGRESS="${BENCH_DISABLE_INGRESS}"
export AE_NODE_NOTREADY_AFTER="${bench_node_notready_after}"
export BENCH_ENV_DIR="$env_dir"
export BENCH_CONTROLLER_PID_FILE="$pid_file"
export BENCH_CONTROLLER_PID="$controller_pid"
export BENCH_CONTROLLER_SUDO="$([[ "$controller_mode" == "sudo" ]] && echo 1 || echo 0)"
export BENCH_CONTROLLER_LOG="$log_file"
export BENCH_SPEC_DIR="$spec_dir"
export BENCH_APPLY_DIR="$apply_dir"
export BENCH_PRIMARY_MANIFEST="$primary_manifest_path"
export BENCH_PRIMARY_APP="$primary_app_name"
export BENCH_METRICS_PORT="$metrics_port"
export PYTHON_BIN="$python_bin"
export WAIT_READY_TRIES="${WAIT_READY_TRIES:-180}"
export WAIT_READY_DELAY="${WAIT_READY_DELAY:-2}"
EOF

chmod 600 "$env_file"
printf '%s\n' "$env_file"
