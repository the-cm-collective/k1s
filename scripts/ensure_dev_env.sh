#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DEV_ENV_FILE:-$ROOT_DIR/state/dev.env}"

detect_engine() {
  if [[ -n "${AE_CONTAINER_CLI:-}" ]]; then
    printf '%s' "${AE_CONTAINER_CLI}"
    return 0
  fi
  if [[ -n "${STACK_BIN:-}" ]]; then
    printf '%s' "${STACK_BIN}"
    return 0
  fi
  case "${AE_RUNTIME_BACKEND:-}" in
    docker) printf 'docker'; return 0 ;;
    podman|oci) printf 'podman'; return 0 ;;
  esac
  if command -v podman >/dev/null 2>&1; then
    printf 'podman'
    return 0
  fi
  printf 'docker'
}

engine="$(detect_engine)"
host_alias="host.docker.internal"
if [[ "$engine" == "podman" ]]; then
  host_alias="host.containers.internal"
fi

apishim_port="${APISHIM_PORT:-8445}"
apishim_upstream="${APISHIM_UPSTREAM:-${host_alias}:${apishim_port}}"

mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" <<EOF
APISHIM_PORT=${apishim_port}
APISHIM_UPSTREAM=${apishim_upstream}
EOF
