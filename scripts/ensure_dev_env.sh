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
apishim_container_port="${APISHIM_CONTAINER_PORT:-8445}"
if [[ -n "${POSTGRES_PORT:-}" && "${apishim_port}" == "${POSTGRES_PORT}" ]]; then
  apishim_port="8445"
fi
if [[ "${apishim_port}" == "5432" ]]; then
  apishim_port="8445"
fi
apishim_container="${APISHIM_CONTAINER:-0}"
if [[ "${apishim_container}" == "1" ]]; then
  apishim_upstream="apishim:${apishim_container_port}"
else
  apishim_upstream="${APISHIM_UPSTREAM:-${host_alias}:${apishim_port}}"
fi

mkdir -p "$(dirname "$ENV_FILE")"
{
  printf 'APISHIM_PORT=%s\n' "${apishim_port}"
  printf 'APISHIM_CONTAINER_PORT=%s\n' "${apishim_container_port}"
  printf 'CADDY_HOST_ALIAS=%s\n' "${host_alias}"
  printf 'APISHIM_UPSTREAM=%s\n' "${apishim_upstream}"
} > "$ENV_FILE"
