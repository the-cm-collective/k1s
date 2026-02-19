#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APISHIM_ENV_FILE:-$ROOT_DIR/state/profiles/labs/apishim.env}"
if [[ -n "${APISHIM_CLI_ENV_FILE:-}" ]]; then
  CLI_ENV_FILE="${APISHIM_CLI_ENV_FILE}"
else
  CLI_ENV_FILE="$(dirname "$ENV_FILE")/apishim.cli.env"
fi
CERT_FILE="${APISHIM_CERT_FILE:-$(dirname "$ENV_FILE")/apishim.crt}"
if [[ -n "${APISHIM_CLI_CA_FILE:-}" ]]; then
  CLI_CA_FILE="${APISHIM_CLI_CA_FILE}"
else
  CLI_CA_FILE="$(dirname "$CLI_ENV_FILE")/apishim.ca.crt"
fi
SHARED_GROUP="${AE_CLI_SHARED_GROUP:-aecli}"

log() {
  printf '[apishim-cli-env] %s\n' "$1"
}

read_env_var() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  awk -F= -v k="$key" '
    $1 ~ "^[[:space:]]*"k"[[:space:]]*$" {
      sub(/^[[:space:]]*[^=]+[[:space:]]*=[[:space:]]*/, "", $0)
      gsub(/^[[:space:]]*"/, "", $0)
      gsub(/"[[:space:]]*$/, "", $0)
      gsub(/^[[:space:]]*'\''/, "", $0)
      gsub(/'\''[[:space:]]*$/, "", $0)
      print $0
      exit
    }
  ' "$file"
}

if [[ ! -f "$ENV_FILE" ]]; then
  log "source env missing: $ENV_FILE"
  exit 1
fi

server="$(read_env_var "AE_APISHIM_SERVER" "$ENV_FILE" || true)"
if [[ -z "$server" ]]; then
  server="${AE_APISHIM_SERVER:-}"
fi
if [[ -z "$server" ]]; then
  port_hint="$(read_env_var "APISHIM_PORT" "$ROOT_DIR/state/dev.env" || true)"
  if [[ -n "$port_hint" ]]; then
    server="https://127.0.0.1:${port_hint}"
  else
    server="https://127.0.0.1:8445"
  fi
fi

mint_token="$(read_env_var "AE_APISHIM_MINT_TOKEN" "$ENV_FILE" || true)"
if [[ -z "$mint_token" ]]; then
  mint_token="${AE_APISHIM_MINT_TOKEN:-}"
fi
if [[ -z "$mint_token" ]]; then
  log "AE_APISHIM_MINT_TOKEN is missing in $ENV_FILE"
  exit 1
fi

apply_shared_group() {
  local target="$1"
  if command -v getent >/dev/null 2>&1 && getent group "$SHARED_GROUP" >/dev/null 2>&1; then
    if chgrp "$SHARED_GROUP" "$target" 2>/dev/null; then
      :
    elif [[ -n "${SUDO_GID:-}" && "${SUDO_GID}" =~ ^[0-9]+$ ]]; then
      chgrp "$SUDO_GID" "$target" 2>/dev/null || true
    fi
  else
    log "group '$SHARED_GROUP' not found (or getent unavailable); keeping current file group for $target"
  fi
}

mkdir -p "$(dirname "$CLI_ENV_FILE")"
mkdir -p "$(dirname "$CLI_CA_FILE")"
umask 027

ca_bundle=""
if [[ -f "$CERT_FILE" ]]; then
  if cp "$CERT_FILE" "$CLI_CA_FILE" 2>/dev/null; then
    chmod 640 "$CLI_CA_FILE" 2>/dev/null || true
    apply_shared_group "$CLI_CA_FILE"
    if [[ -r "$CLI_CA_FILE" ]]; then
      ca_bundle="$CLI_CA_FILE"
    else
      log "shared CA file exists but is not readable: $CLI_CA_FILE"
    fi
  else
    log "warning: failed to sync shared CA bundle from $CERT_FILE"
  fi
else
  log "warning: cert file missing; skipping CA bundle export: $CERT_FILE"
fi

{
  printf 'AE_APISHIM_SERVER=%s\n' "$server"
  printf 'AE_APISHIM_MINT_TOKEN=%s\n' "$mint_token"
  if [[ -n "$ca_bundle" ]]; then
    printf 'AE_APISHIM_CA_BUNDLE=%s\n' "$ca_bundle"
  fi
} > "$CLI_ENV_FILE"
chmod 640 "$CLI_ENV_FILE"
apply_shared_group "$CLI_ENV_FILE"

log "Wrote $CLI_ENV_FILE (mint-only shared auth env)."
