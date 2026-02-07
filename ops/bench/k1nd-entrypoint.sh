#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/state}"
SPECS_DIR="${AE_SPECS_DIR:-/specs}"
APISHIM_PORT="${APISHIM_PORT:-8445}"
API_PORT="${AE_METRICS_PORT:-9108}"

mkdir -p "$STATE_DIR" "$SPECS_DIR"
mkdir -p "$STATE_DIR/caddy" "$STATE_DIR/caddy-data"

if [[ -e /data && ! -L /data ]]; then
  rm -rf /data
fi
if [[ ! -e /data ]]; then
  ln -s "$STATE_DIR/caddy-data" /data
fi

if [[ -e /etc/caddy/dynsites && ! -L /etc/caddy/dynsites ]]; then
  rm -rf /etc/caddy/dynsites
fi
if [[ ! -e /etc/caddy/dynsites ]]; then
  ln -s "$STATE_DIR/caddy" /etc/caddy/dynsites
fi

export APISHIM_ENV_FILE="${APISHIM_ENV_FILE:-$STATE_DIR/apishim.env}"
export APISHIM_CERT_FILE="${APISHIM_CERT_FILE:-$STATE_DIR/apishim.crt}"
export APISHIM_KEY_FILE="${APISHIM_KEY_FILE:-$STATE_DIR/apishim.key}"
export AE_APISHIM_DB="${AE_APISHIM_DB:-$STATE_DIR/apishim.db}"
export AE_STATE_DB="${AE_STATE_DB:-$STATE_DIR/controller.db}"
export AE_SPECS_DIR="${AE_SPECS_DIR:-$SPECS_DIR}"
export AE_CADDY_SITES="${AE_CADDY_SITES:-$STATE_DIR/caddy}"
export AE_CADDY_FILE="${AE_CADDY_FILE:-/etc/caddy/Caddyfile}"
export AE_CADDY_BIN="${AE_CADDY_BIN:-/usr/bin/caddy}"
export CADDY_HOST_ALIAS="${CADDY_HOST_ALIAS:-127.0.0.1}"
export APISHIM_UPSTREAM="${APISHIM_UPSTREAM:-127.0.0.1:${APISHIM_PORT}}"
export AE_APISHIM_ENABLE="${AE_APISHIM_ENABLE:-1}"
export AE_APISHIM_ALLOW_ANON="${AE_APISHIM_ALLOW_ANON:-0}"
export AE_APISHIM_RBAC="${AE_APISHIM_RBAC:-1}"
export AE_APISHIM_RBAC_EVAL="${AE_APISHIM_RBAC_EVAL:-0}"

# Generate shim tokens + TLS certs (self-signed) if needed.
if [[ -x "/workspace/scripts/ensure_apishim_env.sh" ]]; then
  /workspace/scripts/ensure_apishim_env.sh >/dev/null 2>&1 || true
fi

python -m ae.apishim serve --host 0.0.0.0 --port "$APISHIM_PORT" --tls >"$STATE_DIR/apishim.log" 2>&1 &
shim_pid=$!
if command -v caddy >/dev/null 2>&1; then
  caddy run --config "${AE_CADDY_FILE}" >"$STATE_DIR/caddy.log" 2>&1 &
  caddy_pid=$!
else
  caddy_pid=""
fi
trap '[[ -n "${caddy_pid:-}" ]] && kill "$caddy_pid" >/dev/null 2>&1 || true; kill "$shim_pid" >/dev/null 2>&1 || true' TERM INT

exec python -m ae.controller --loop --specs "$AE_SPECS_DIR" --metrics-port "$API_PORT" --watch
