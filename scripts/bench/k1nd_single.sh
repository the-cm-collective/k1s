#!/usr/bin/env bash
set -euo pipefail

cmd=${1:-}
case "$cmd" in
  up|down|wait|status)
    ;;
  -h|--help|help|'')
    cat <<'USAGE'
Usage: scripts/bench/k1nd_single.sh <up|down|wait|status>

Environment:
  K1ND_COMPOSE_FILE   Compose file (default: ops/bench/k1nd-compose.yaml)
  K1ND_STATE_DIR      Host state dir (default: state/bench-k1nd-state)
  K1ND_SPECS_DIR      Host specs dir (default: state/bench-k1nd-specs)
  K1ND_APPLY_DIR      Host apply dir (default: state/bench-k1nd-apply)
  K1ND_MANIFEST       Optional manifest to copy into apply dir
  K1ND_SEED_SPECS     If set to 1, also copy manifest into specs dir
  K1ND_API_PORT       Host API port (default: 9108)
  K1ND_APISHIM_PORT   Host apishim port (default: 8445)
  K1ND_WAIT_TRIES     Wait attempts (default: 30)
  K1ND_WAIT_DELAY     Wait delay seconds (default: 2)
USAGE
    exit 0
    ;;
  *)
    echo "[k1nd] unknown command: $cmd" >&2
    exit 2
    ;;
esac

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

abs_path() {
  local p="$1"
  if command -v python >/dev/null 2>&1; then
    python - "$p" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
  else
    if [[ "$p" = /* ]]; then
      echo "$p"
    else
      echo "$repo_root/$p"
    fi
  fi
}

compose_file="${K1ND_COMPOSE_FILE:-ops/bench/k1nd-compose.yaml}"
state_dir="${K1ND_STATE_DIR:-state/bench-k1nd-state}"
specs_dir="${K1ND_SPECS_DIR:-state/bench-k1nd-specs}"
apply_dir="${K1ND_APPLY_DIR:-state/bench-k1nd-apply}"

port_is_free() {
  local port="$1"
  python - "$port" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

choose_port() {
  local label="$1"
  local requested="$2"
  local start="$3"
  local end="$4"
  local port

  if port_is_free "$requested"; then
    echo "$requested"
    return 0
  fi

  for port in $(seq "$start" "$end"); do
    if port_is_free "$port"; then
      echo "[k1nd] ${label} port ${requested} busy on host; using ${port}" >&2
      echo "$port"
      return 0
    fi
  done

  echo "[k1nd] no free host port found for ${label} in ${start}-${end}" >&2
  exit 2
}

state_dir="$(abs_path "$state_dir")"
specs_dir="$(abs_path "$specs_dir")"
apply_dir="$(abs_path "$apply_dir")"

reset_k1nd_dirs() {
  rm -rf "$state_dir" "$specs_dir" "$apply_dir"
  mkdir -p "$state_dir" "$specs_dir" "$apply_dir"
}

if [[ "$cmd" == "up" ]]; then
  reset_k1nd_dirs
else
  mkdir -p "$state_dir" "$specs_dir" "$apply_dir"
fi

ports_state_file="$state_dir/ports.env"

assign_requested_ports() {
  api_port="${K1ND_API_PORT:-9108}"
  apishim_port="${K1ND_APISHIM_PORT:-8445}"
  caddy_http_port="${K1ND_CADDY_HTTP_PORT:-8888}"
  caddy_https_port="${K1ND_CADDY_HTTPS_PORT:-8443}"
}

select_ports() {
  api_port="$(choose_port api "${K1ND_API_PORT:-9108}" "${K1ND_API_PORT_START:-19108}" "${K1ND_API_PORT_END:-19208}")"
  apishim_port="$(choose_port apishim "${K1ND_APISHIM_PORT:-8445}" "${K1ND_APISHIM_PORT_START:-18445}" "${K1ND_APISHIM_PORT_END:-18545}")"
  caddy_http_port="$(choose_port caddy-http "${K1ND_CADDY_HTTP_PORT:-8888}" "${K1ND_CADDY_HTTP_PORT_START:-18888}" "${K1ND_CADDY_HTTP_PORT_END:-18988}")"
  caddy_https_port="$(choose_port caddy-https "${K1ND_CADDY_HTTPS_PORT:-8443}" "${K1ND_CADDY_HTTPS_PORT_START:-18443}" "${K1ND_CADDY_HTTPS_PORT_END:-18543}")"
}

save_port_state() {
  cat >"$ports_state_file" <<EOF
export K1ND_API_PORT="$api_port"
export K1ND_APISHIM_PORT="$apishim_port"
export K1ND_CADDY_HTTP_PORT="$caddy_http_port"
export K1ND_CADDY_HTTPS_PORT="$caddy_https_port"
EOF
}

load_port_state() {
  if [[ ! -f "$ports_state_file" ]]; then
    return 1
  fi
  # shellcheck disable=SC1090
  source "$ports_state_file"
  api_port="${K1ND_API_PORT:-}"
  apishim_port="${K1ND_APISHIM_PORT:-}"
  caddy_http_port="${K1ND_CADDY_HTTP_PORT:-}"
  caddy_https_port="${K1ND_CADDY_HTTPS_PORT:-}"
  [[ -n "$api_port" && -n "$apishim_port" && -n "$caddy_http_port" && -n "$caddy_https_port" ]]
}

if [[ "$cmd" == "up" ]]; then
  select_ports
  save_port_state
elif ! load_port_state; then
  assign_requested_ports
fi

export K1ND_STATE_DIR="$state_dir"
export K1ND_SPECS_DIR="$specs_dir"
export K1ND_APPLY_DIR="$apply_dir"
export K1ND_API_PORT="$api_port"
export K1ND_APISHIM_PORT="$apishim_port"
export K1ND_CADDY_HTTP_PORT="$caddy_http_port"
export K1ND_CADDY_HTTPS_PORT="$caddy_https_port"

if [[ -n "${K1ND_MANIFEST:-}" ]]; then
  if [[ -f "$K1ND_MANIFEST" ]]; then
    base=$(basename "$K1ND_MANIFEST")
    cp -f "$K1ND_MANIFEST" "$apply_dir/$base"
    if [[ "${K1ND_SEED_SPECS:-0}" == "1" ]]; then
      cp -f "$K1ND_MANIFEST" "$specs_dir/$base"
    fi
  else
    echo "[k1nd] manifest not found: $K1ND_MANIFEST" >&2
    exit 2
  fi
fi

compose_args=(docker compose -f "$compose_file")
if ! command -v docker >/dev/null 2>&1; then
  echo "[k1nd] docker is required for k1nd single-container runs" >&2
  exit 2
fi

case "$cmd" in
  up)
    "${compose_args[@]}" up -d --build
    ;;
  down)
    "${compose_args[@]}" down --remove-orphans
    rm -f "$ports_state_file"
    ;;
  status)
    "${compose_args[@]}" ps
    ;;
  wait)
    tries="${K1ND_WAIT_TRIES:-30}"
    delay="${K1ND_WAIT_DELAY:-2}"
    for ((i=1; i<=tries; i++)); do
      if curl -fsS "http://127.0.0.1:${api_port}/health" >/dev/null 2>&1; then
        exit 0
      fi
      sleep "$delay"
    done
    echo "[k1nd] controller not ready after $((tries * delay))s" >&2
    exit 1
    ;;
esac
