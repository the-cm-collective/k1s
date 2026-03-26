#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACTION="${1:-${AE_DEV_LOCAL_ACTION:-apply}}"
HOSTS_IP="${DEV_LOCAL_HOSTS_IP:-127.0.0.1}"
HOSTS_BLOCK_BEGIN="# BEGIN k1s-local-dev"
HOSTS_BLOCK_END="# END k1s-local-dev"
COMBINED_DEV_CA="${ROOT_DIR}/state/certs/combined-dev-ca.pem"
source "${ROOT_DIR}/scripts/lib/nixos_bridge.sh"
NIXOS_BRIDGE_ROOT="$(k1s_nixos_bridge_root)"
NIXOS_BRIDGE_HOSTS_FILE="$(k1s_nixos_bridge_hosts_file)"
NIXOS_BRIDGE_CERT_DIR="$(k1s_nixos_bridge_cert_dir)"
NIXOS_MODULE_DEST="$(k1s_nixos_module_dest)"
NIXOS_FLAKE="$(k1s_nixos_flake)"
NIXOS_HOST="$(k1s_nixos_host)"
NIXOS_REBUILD="$(k1s_nixos_rebuild_mode)"
HAS_TTY=0

declare -A TRUST_SOURCES=()
declare -A NSS_LABELS=()
MANAGED_CERT_FILES=(
  "caddy-local-root.crt"
  "k1s-apishim-ca.crt"
  "k1s-envoy-fallback.crt"
)

if [[ -t 0 && -t 1 ]]; then
  HAS_TTY=1
fi

OS_ID=""
TRUST_BACKEND="none"
TRUST_DEST_DIR=""
TRUST_UPDATE_CMD=()
PRIV_PREFIX=()
NEEDS_ATTENTION=0

detect_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    printf '%s' "${PYTHON_BIN}"
    return 0
  fi
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s' "$ROOT_DIR/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "python3"
    return 0
  fi
  printf '%s' "python"
}

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
    printf '%s' "podman"
    return 0
  fi
  printf '%s' "docker"
}

log() {
  printf '[dev-local] %s\n' "$1"
}

warn() {
  NEEDS_ATTENTION=1
  printf '[dev-local] warning: %s\n' "$1" >&2
}

prompt_yes_no() {
  local msg="$1"
  local default="${2:-Y}"
  local prompt="" reply=""
  if [[ "$default" == "Y" || "$default" == "y" ]]; then
    prompt="$msg [Y/n]: "
  else
    prompt="$msg [y/N]: "
  fi
  read -r -p "$prompt" reply || reply=""
  if [[ -z "$reply" ]]; then
    reply="$default"
  fi
  case "$reply" in
    Y|y|Yes|yes) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_privileges() {
  if [[ "${EUID}" -eq 0 ]]; then
    PRIV_PREFIX=()
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    PRIV_PREFIX=(sudo -n)
    return 0
  fi
  if [[ "${HAS_TTY}" != "1" ]]; then
    warn "sudo access is required for host DNS/trust changes; rerun interactively or as root"
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    warn "sudo is not available; cannot apply host DNS/trust changes"
    return 1
  fi
  log "requesting sudo to apply host DNS/trust changes"
  sudo -v
  PRIV_PREFIX=(sudo)
}

run_priv() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
    return 0
  fi
  if [[ ${#PRIV_PREFIX[@]} -eq 0 ]]; then
    return 1
  fi
  "${PRIV_PREFIX[@]}" "$@"
}

load_os_release() {
  OS_ID="$(k1s_os_id /etc/os-release 2>/dev/null || true)"
}

is_nixos() {
  k1s_is_nixos /etc/os-release
}

detect_trust_backend() {
  if command -v update-ca-certificates >/dev/null 2>&1; then
    TRUST_BACKEND="debian"
    TRUST_DEST_DIR="/usr/local/share/ca-certificates"
    TRUST_UPDATE_CMD=(update-ca-certificates)
    return 0
  fi
  if command -v update-ca-trust >/dev/null 2>&1; then
    TRUST_BACKEND="fedora"
    TRUST_DEST_DIR="/etc/pki/ca-trust/source/anchors"
    TRUST_UPDATE_CMD=(update-ca-trust extract)
    return 0
  fi
  TRUST_BACKEND="none"
  TRUST_DEST_DIR=""
  TRUST_UPDATE_CMD=()
}

dedupe_words() {
  local item=""
  local -A seen=()
  local -a ordered=()
  for item in "$@"; do
    [[ -n "$item" ]] || continue
    if [[ -z "${seen[$item]:-}" ]]; then
      seen["$item"]=1
      ordered+=("$item")
    fi
  done
  printf '%s\n' "${ordered[*]}"
}

default_hosts() {
  local -a hosts=(
    "docs.home.arpa"
    "api.home.arpa"
    "dash.home.arpa"
    "echo.home.arpa"
  )
  if [[ "${AE_DEMO_SEED:-0}" == "1" || "${AE_DEMO_MODE:-0}" == "1" ]]; then
    hosts=(
      "blue.home.arpa"
      "green.home.arpa"
      "${hosts[@]}"
    )
  fi
  dedupe_words "${hosts[@]}"
}

load_hosts() {
  local hosts_raw="${DEV_LOCAL_HOSTS:-}"
  local item=""
  if [[ -z "${hosts_raw// }" ]]; then
    hosts_raw="$(default_hosts)"
  fi
  hosts_raw="$(dedupe_words ${hosts_raw})"
  HOSTS=()
  for item in ${hosts_raw}; do
    HOSTS+=("$item")
  done
}

strip_managed_hosts_block() {
  local src="$1"
  awk -v begin="$HOSTS_BLOCK_BEGIN" -v end="$HOSTS_BLOCK_END" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { print }
  ' "$src"
}

write_file_if_changed() {
  local src="$1"
  local dst="$2"
  local changed_var="$3"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    return 0
  fi
  run_priv install -D -m 0644 "$src" "$dst"
  printf -v "$changed_var" "1"
  return 0
}

remove_file_if_present() {
  local path="$1"
  local changed_var="$2"
  if [[ -e "$path" ]]; then
    run_priv rm -f "$path"
    printf -v "$changed_var" "1"
  fi
}

ensure_state_certs_dir() {
  mkdir -p "${ROOT_DIR}/state/certs"
}

discover_caddy_ca() {
  local https_port="${CADDY_HTTPS_PORT:-8443}"
  local root_ca_primary="${ROOT_DIR}/state/caddy-data/caddy/pki/authorities/local/root.crt"
  local root_ca_legacy="${ROOT_DIR}/state/caddy-data/pki/authorities/local/root.crt"
  local root_ca=""
  local copied="${ROOT_DIR}/state/certs/caddy-local-root.crt"
  local engine="" container=""
  local touched=0

  ensure_state_certs_dir
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

  if [[ -z "$root_ca" ]]; then
    engine="$(detect_engine)"
    container="${AE_CADDY_CONTAINER:-}"
    if [[ -z "$container" ]]; then
      container="$("$engine" ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | awk '/caddy/ {print $1; exit}' || true)"
    fi
    if [[ -n "$container" ]]; then
      "$engine" cp "${container}":/data/caddy/pki/authorities/local/root.crt "$copied" >/dev/null 2>&1 || true
      if [[ -s "$copied" ]]; then
        root_ca="$copied"
      fi
    fi
  fi

  if [[ -n "$root_ca" && "$root_ca" != "$copied" ]]; then
    cp -f "$root_ca" "$copied"
    root_ca="$copied"
  fi

  if [[ -s "$root_ca" ]]; then
    printf '%s' "$root_ca"
    return 0
  fi
  if [[ "$touched" -eq 1 ]]; then
    warn "Caddy local CA not found yet; Caddy may still be starting"
  else
    warn "Caddy local CA not found; Caddy may not be running"
  fi
  return 1
}

discover_apishim_ca() {
  local ca_cert="${AE_APISHIM_TLS_CA_CERT:-}"
  local leaf_cert="${AE_APISHIM_TLS_CERT:-}"
  if [[ -z "$ca_cert" && -n "${DEV_PROFILE_DIR:-}" && -s "${DEV_PROFILE_DIR}/apishim.ca.crt" ]]; then
    ca_cert="${DEV_PROFILE_DIR}/apishim.ca.crt"
  fi
  if [[ -z "$leaf_cert" && -n "${DEV_PROFILE_DIR:-}" && -s "${DEV_PROFILE_DIR}/apishim.crt" ]]; then
    leaf_cert="${DEV_PROFILE_DIR}/apishim.crt"
  fi
  if [[ -n "$ca_cert" && -s "$ca_cert" ]]; then
    printf '%s' "$ca_cert"
    return 0
  fi
  if [[ -n "$leaf_cert" && -s "$leaf_cert" ]]; then
    printf '%s' "$leaf_cert"
    return 0
  fi
  return 1
}

discover_envoy_cert() {
  local tls_dir="${AE_TLS_DIR:-${ROOT_DIR}/state/tls}"
  local envoy_cert="${tls_dir}/envoy-fallback.crt"
  if [[ -s "$envoy_cert" ]]; then
    printf '%s' "$envoy_cert"
    return 0
  fi
  return 1
}

collect_trust_sources() {
  local path=""
  TRUST_SOURCES=()
  NSS_LABELS=()

  if path="$(discover_caddy_ca || true)" && [[ -n "$path" && -s "$path" ]]; then
    TRUST_SOURCES["caddy-local-root.crt"]="$path"
    NSS_LABELS["caddy-local-root.crt"]="Caddy Local Root"
  fi
  if path="$(discover_apishim_ca || true)" && [[ -n "$path" && -s "$path" ]]; then
    TRUST_SOURCES["k1s-apishim-ca.crt"]="$path"
    NSS_LABELS["k1s-apishim-ca.crt"]="k1s Apishim Dev CA"
  fi
  if path="$(discover_envoy_cert || true)" && [[ -n "$path" && -s "$path" ]]; then
    TRUST_SOURCES["k1s-envoy-fallback.crt"]="$path"
    NSS_LABELS["k1s-envoy-fallback.crt"]="k1s Envoy Fallback"
  fi
}

build_combined_dev_bundle() {
  local py_bin=""
  local -a cert_args=()
  local label=""
  if [[ "${#TRUST_SOURCES[@]}" -eq 0 ]]; then
    return 0
  fi
  ensure_state_certs_dir
  for label in "${MANAGED_CERT_FILES[@]}"; do
    if [[ -n "${TRUST_SOURCES[$label]:-}" ]]; then
      cert_args+=("${TRUST_SOURCES[$label]}")
    fi
  done
  py_bin="$(detect_python)"
  "$py_bin" - "$COMBINED_DEV_CA" "${cert_args[@]}" <<'PY' || true
import os
import ssl
import sys

out_path = sys.argv[1]
certs = [p for p in sys.argv[2:] if os.path.exists(p)]
base = ssl.get_default_verify_paths().cafile
if not base or not os.path.exists(base):
    try:
        import certifi  # type: ignore
    except Exception:
        certifi = None
    if certifi is not None:
        base = certifi.where()

data = b""
if base and os.path.exists(base):
    with open(base, "rb") as fh:
        data = fh.read()

with open(out_path, "wb") as out:
    if data:
        out.write(data)
        if not data.endswith(b"\n"):
            out.write(b"\n")
    seen = data
    for cert in certs:
      with open(cert, "rb") as fh:
          payload = fh.read()
      if payload not in seen:
          out.write(payload)
          if not payload.endswith(b"\n"):
              out.write(b"\n")
          seen += payload
PY
  if [[ -s "$COMBINED_DEV_CA" ]]; then
    log "wrote combined dev CA bundle to ${COMBINED_DEV_CA}"
  fi
}

sync_nss_trust() {
  local label="" cert="" nickname="" prof=""
  if ! command -v certutil >/dev/null 2>&1; then
    return 0
  fi
  mkdir -p "$HOME/.pki/nssdb"
  for label in "${MANAGED_CERT_FILES[@]}"; do
    cert="${TRUST_SOURCES[$label]:-}"
    [[ -n "$cert" && -s "$cert" ]] || continue
    nickname="${NSS_LABELS[$label]}"
    certutil -d sql:"$HOME/.pki/nssdb" -D -n "$nickname" 2>/dev/null || true
    certutil -d sql:"$HOME/.pki/nssdb" -A -t "C,," -n "$nickname" -i "$cert" 2>/dev/null || true
    for prof in "$HOME"/.mozilla/firefox/*.default* "$HOME"/.mozilla/firefox/*.dev*; do
      [[ -d "$prof" ]] || continue
      certutil -d sql:"$prof" -D -n "$nickname" 2>/dev/null || true
      certutil -d sql:"$prof" -A -t "C,," -n "$nickname" -i "$cert" 2>/dev/null || true
    done
  done
  log "updated NSS/Firefox trust (restart browsers if already open)"
}

clean_nss_trust() {
  local label="" nickname="" prof=""
  if ! command -v certutil >/dev/null 2>&1; then
    return 0
  fi
  for label in "${MANAGED_CERT_FILES[@]}"; do
    nickname="${NSS_LABELS[$label]:-}"
    if [[ -z "$nickname" ]]; then
      case "$label" in
        caddy-local-root.crt) nickname="Caddy Local Root" ;;
        k1s-apishim-ca.crt) nickname="k1s Apishim Dev CA" ;;
        k1s-envoy-fallback.crt) nickname="k1s Envoy Fallback" ;;
      esac
    fi
    certutil -d sql:"$HOME/.pki/nssdb" -D -n "$nickname" 2>/dev/null || true
    for prof in "$HOME"/.mozilla/firefox/*.default* "$HOME"/.mozilla/firefox/*.dev*; do
      [[ -d "$prof" ]] || continue
      certutil -d sql:"$prof" -D -n "$nickname" 2>/dev/null || true
    done
  done
}

apply_direct_hosts() {
  local tmp
  if ! ensure_privileges; then
    return 0
  fi
  tmp="$(mktemp)"
  strip_managed_hosts_block /etc/hosts >"$tmp"
  {
    printf '\n%s\n' "$HOSTS_BLOCK_BEGIN"
    for host in "${HOSTS[@]}"; do
      printf '%s %s\n' "$HOSTS_IP" "$host"
    done
    printf '%s\n' "$HOSTS_BLOCK_END"
  } >>"$tmp"
  if cmp -s "$tmp" /etc/hosts; then
    rm -f "$tmp"
    return 0
  fi
  log "updating /etc/hosts for ${HOSTS[*]}"
  run_priv install -m 0644 "$tmp" /etc/hosts
  rm -f "$tmp"
}

clean_direct_hosts() {
  local tmp
  if ! ensure_privileges; then
    return 0
  fi
  tmp="$(mktemp)"
  strip_managed_hosts_block /etc/hosts >"$tmp"
  if cmp -s "$tmp" /etc/hosts; then
    rm -f "$tmp"
    return 0
  fi
  log "removing managed /etc/hosts entries"
  run_priv install -m 0644 "$tmp" /etc/hosts
  rm -f "$tmp"
}

sync_direct_trust() {
  local changed=0
  local label="" src="" dst=""
  if [[ "$TRUST_BACKEND" == "none" ]]; then
    warn "no system trust updater found; relying on NSS/browser trust and ${COMBINED_DEV_CA}"
    return 0
  fi
  if ! ensure_privileges; then
    return 0
  fi
  run_priv mkdir -p "$TRUST_DEST_DIR"
  for label in "${MANAGED_CERT_FILES[@]}"; do
    src="${TRUST_SOURCES[$label]:-}"
    dst="${TRUST_DEST_DIR}/${label}"
    if [[ -n "$src" && -s "$src" ]]; then
      if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        log "installing ${label} into system trust"
        run_priv install -m 0644 "$src" "$dst"
        changed=1
      fi
    elif [[ -e "$dst" ]]; then
      run_priv rm -f "$dst"
      changed=1
    fi
  done
  if [[ "$changed" -eq 1 ]]; then
    run_priv "${TRUST_UPDATE_CMD[@]}" >/dev/null 2>&1 || true
  fi
}

clean_direct_trust() {
  local changed=0
  local label="" dst=""
  if [[ "$TRUST_BACKEND" == "none" ]]; then
    return 0
  fi
  if ! ensure_privileges; then
    return 0
  fi
  for label in "${MANAGED_CERT_FILES[@]}"; do
    dst="${TRUST_DEST_DIR}/${label}"
    if [[ -e "$dst" ]]; then
      run_priv rm -f "$dst"
      changed=1
    fi
  done
  if [[ "$changed" -eq 1 ]]; then
    log "removing managed system trust certificates"
    run_priv "${TRUST_UPDATE_CMD[@]}" >/dev/null 2>&1 || true
  fi
}

nixos_bridge_imported() {
  k1s_nixos_bridge_imported /etc/nixos "$NIXOS_MODULE_DEST"
}

print_nixos_bridge_bootstrap() {
  warn "NixOS bridge not detected; install it once before expecting persistent /etc/hosts and system CA updates"
  k1s_nixos_bootstrap_instructions "$ROOT_DIR" "$NIXOS_MODULE_DEST" "$NIXOS_FLAKE" "$NIXOS_HOST" >&2
}

sync_nixos_state() {
  local changed=0
  local tmp="" label="" src="" dst=""

  if ! ensure_privileges; then
    return 0
  fi

  run_priv mkdir -p "$NIXOS_BRIDGE_CERT_DIR"

  tmp="$(mktemp)"
  for host in "${HOSTS[@]}"; do
    printf '%s %s\n' "$HOSTS_IP" "$host" >>"$tmp"
  done
  write_file_if_changed "$tmp" "$NIXOS_BRIDGE_HOSTS_FILE" changed
  rm -f "$tmp"

  for label in "${MANAGED_CERT_FILES[@]}"; do
    src="${TRUST_SOURCES[$label]:-}"
    dst="${NIXOS_BRIDGE_CERT_DIR}/${label}"
    if [[ -n "$src" && -s "$src" ]]; then
      if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        log "syncing ${label} into ${NIXOS_BRIDGE_CERT_DIR}"
        run_priv install -m 0644 "$src" "$dst"
        changed=1
      fi
    elif [[ -e "$dst" ]]; then
      run_priv rm -f "$dst"
      changed=1
    fi
  done

  NIXOS_STATE_CHANGED="$changed"
}

clean_nixos_state() {
  local changed=0
  local label=""
  if ! ensure_privileges; then
    return 0
  fi
  remove_file_if_present "$NIXOS_BRIDGE_HOSTS_FILE" changed
  for label in "${MANAGED_CERT_FILES[@]}"; do
    remove_file_if_present "${NIXOS_BRIDGE_CERT_DIR}/${label}" changed
  done
  NIXOS_STATE_CHANGED="$changed"
}

maybe_rebuild_nixos() {
  local mode="${NIXOS_REBUILD}"
  local cmd=(nixos-rebuild switch --impure --flake "${NIXOS_FLAKE}#${NIXOS_HOST}")
  if [[ "${NIXOS_STATE_CHANGED:-0}" != "1" ]]; then
    return 0
  fi
  if ! nixos_bridge_imported; then
    print_nixos_bridge_bootstrap
    return 0
  fi
  case "$mode" in
    never)
      log "NixOS bridge state changed; run: sudo ${cmd[*]}"
      return 0
      ;;
    always)
      log "rebuilding NixOS to apply local DNS/TLS bridge"
      run_priv "${cmd[@]}"
      return 0
      ;;
    prompt|*)
      if [[ "${HAS_TTY}" != "1" ]]; then
        log "NixOS bridge state changed; run: sudo ${cmd[*]}"
        return 0
      fi
      if prompt_yes_no "Run nixos-rebuild switch to apply local DNS/TLS updates?" Y; then
        log "rebuilding NixOS to apply local DNS/TLS bridge"
        run_priv "${cmd[@]}"
      else
        log "NixOS bridge state changed; run later: sudo ${cmd[*]}"
      fi
      ;;
  esac
}

apply_mode() {
  collect_trust_sources
  build_combined_dev_bundle
  sync_nss_trust
  if is_nixos; then
    if ! nixos_bridge_imported; then
      print_nixos_bridge_bootstrap
      return 0
    fi
    sync_nixos_state
    maybe_rebuild_nixos
    return 0
  fi
  apply_direct_hosts
  sync_direct_trust
}

clean_mode() {
  clean_nss_trust
  rm -f "$COMBINED_DEV_CA"
  if is_nixos; then
    clean_nixos_state
    maybe_rebuild_nixos
    return 0
  fi
  clean_direct_hosts
  clean_direct_trust
}

load_os_release
detect_trust_backend
load_hosts

case "$ACTION" in
  apply)
    apply_mode
    ;;
  clean)
    clean_mode
    ;;
  *)
    warn "unknown action: ${ACTION}"
    exit 2
    ;;
esac

if [[ "$NEEDS_ATTENTION" -eq 1 ]]; then
  log "completed with follow-up required"
else
  log "done"
fi
