#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SUDO="${SUDO:-sudo}"

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

log() {
  printf '[dev-local] %s\n' "$1"
}

HOSTS_IP="${DEV_LOCAL_HOSTS_IP:-127.0.0.1}"
DEFAULT_HOSTS="docs.home.arpa api.home.arpa dash.home.arpa echo.home.arpa"
HOSTS_LIST="${DEV_LOCAL_HOSTS:-$DEFAULT_HOSTS}"

ensure_hosts() {
  if [[ -z "${HOSTS_LIST// }" ]]; then
    return 0
  fi
  for host in $HOSTS_LIST; do
    if grep -qE "[[:space:]]${host}$" /etc/hosts; then
      continue
    fi
    log "adding /etc/hosts entry for ${host}"
    $SUDO sh -c "printf '%s %s\n' '${HOSTS_IP}' '${host}' >> /etc/hosts"
  done
}

install_ca() {
  local label="$1"
  local src="$2"
  local dst="/usr/local/share/ca-certificates/${label}.crt"
  if [[ ! -s "$src" ]]; then
    return 1
  fi
  log "installing ${label} into system trust"
  $SUDO cp -f "$src" "$dst"
  $SUDO update-ca-certificates >/dev/null 2>&1 || true
  return 0
}

trust_caddy_ca() {
  if ! command -v update-ca-certificates >/dev/null 2>&1; then
    log "update-ca-certificates not found; skipping system trust install"
    return 0
  fi
  local https_port="${CADDY_HTTPS_PORT:-8443}"
  # Trigger Caddy to mint a local CA once (ignore TLS verification)
  local root_ca_primary="${ROOT_DIR}/state/caddy-data/caddy/pki/authorities/local/root.crt"
  local root_ca_legacy="${ROOT_DIR}/state/caddy-data/pki/authorities/local/root.crt"
  local root_ca="${root_ca_primary}"
  local touched=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -ksS --resolve "docs.home.arpa:${https_port}:${HOSTS_IP}" \
      "https://docs.home.arpa:${https_port}/" >/dev/null 2>&1 || true
    touched=1
    if [[ -s "$root_ca_primary" || -s "$root_ca_legacy" ]]; then
      break
    fi
    sleep 0.3
  done
  if [[ -s "$root_ca_primary" ]]; then
    root_ca="$root_ca_primary"
  elif [[ -s "$root_ca_legacy" ]]; then
    root_ca="$root_ca_legacy"
  fi
  if [[ ! -s "$root_ca" ]]; then
    local engine
    engine="$(detect_engine)"
    local container="${AE_CADDY_CONTAINER:-}"
    if [[ -z "$container" ]]; then
      container="$($engine ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | awk '/caddy/ {print $1; exit}' || true)"
    fi
    if [[ -n "$container" ]]; then
      "$engine" cp "${container}":/data/caddy/pki/authorities/local/root.crt \
        "${ROOT_DIR}/state/certs/caddy-local-root.crt" >/dev/null 2>&1 || true
      root_ca="${ROOT_DIR}/state/certs/caddy-local-root.crt"
    fi
  fi
  if ! install_ca "caddy-local-root" "$root_ca"; then
    if [[ "$touched" -eq 1 ]]; then
      log "Caddy local CA not found yet; Caddy may still be starting"
    else
      log "Caddy local CA not found; Caddy may not be running"
    fi
    log "rerun: make dev-local"
  fi
}

trust_apishim_cert() {
  if ! command -v update-ca-certificates >/dev/null 2>&1; then
    return 0
  fi
  local apishim_cert="${AE_APISHIM_TLS_CERT:-}"
  if [[ -z "$apishim_cert" && -n "${DEV_PROFILE_DIR:-}" ]]; then
    apishim_cert="${DEV_PROFILE_DIR}/apishim.crt"
  fi
  if [[ -z "$apishim_cert" ]]; then
    return 0
  fi
  install_ca "k1s-apishim" "$apishim_cert" || true
}

trust_envoy_cert() {
  if ! command -v update-ca-certificates >/dev/null 2>&1; then
    return 0
  fi
  local tls_dir="${AE_TLS_DIR:-${ROOT_DIR}/state/tls}"
  local envoy_cert="${tls_dir}/envoy-fallback.crt"
  install_ca "k1s-envoy-fallback" "$envoy_cert" || true
}

ensure_hosts
trust_caddy_ca
trust_apishim_cert
trust_envoy_cert
log "done"
