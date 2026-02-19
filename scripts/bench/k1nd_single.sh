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
api_port="${K1ND_API_PORT:-9108}"
apishim_port="${K1ND_APISHIM_PORT:-8445}"

state_dir="$(abs_path "$state_dir")"
specs_dir="$(abs_path "$specs_dir")"
apply_dir="$(abs_path "$apply_dir")"

mkdir -p "$state_dir" "$specs_dir" "$apply_dir"

export K1ND_STATE_DIR="$state_dir"
export K1ND_SPECS_DIR="$specs_dir"
export K1ND_APPLY_DIR="$apply_dir"
export K1ND_API_PORT="$api_port"
export K1ND_APISHIM_PORT="$apishim_port"

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
