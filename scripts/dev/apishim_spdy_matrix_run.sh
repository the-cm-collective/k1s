#!/usr/bin/env bash
set -euo pipefail

RUNTIME="${AE_APISHIM_RUNTIME:-docker}"
TOKEN="${AE_APISHIM_TOKEN:-spdy-token}"
PORT="${AE_APISHIM_PORT:-8445}"
SERVER="https://127.0.0.1:${PORT}"
STATE_DB="${AE_STATE_DB:-/tmp/k1s-ctrl-spdy.db}"
SHIM_DB="${AE_APISHIM_DB:-/tmp/k1s-apishim-spdy.db}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "error: kubectl is required"
  exit 1
fi
if [[ "${RUNTIME}" == "podman" ]] && ! command -v podman >/dev/null 2>&1; then
  echo "error: podman runtime requested but podman is not installed"
  exit 1
fi

CERT="${AE_APISHIM_TLS_CERT:-/tmp/k1s-apishim-tls.crt}"
KEY="${AE_APISHIM_TLS_KEY:-/tmp/k1s-apishim-tls.key}"
if [[ ! -f "${CERT}" || ! -f "${KEY}" ]]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
    -keyout "${KEY}" -out "${CERT}" -subj "/CN=127.0.0.1" -days 1 >/dev/null 2>&1
fi

cleanup() {
  if [[ -n "${SHIM_PID:-}" ]]; then
    kill "${SHIM_PID}" >/dev/null 2>&1 || true
    wait "${SHIM_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${CTRL_PID:-}" ]]; then
    kill "${CTRL_PID}" >/dev/null 2>&1 || true
    wait "${CTRL_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

export PYTHONPATH=src
export AE_STATE_DB="${STATE_DB}"
export AE_RUNTIME_BACKEND="${RUNTIME}"
export AE_CADDY_SITES=""
export AE_APISHIM_ENABLE=1
export AE_APISHIM_TOKEN="${TOKEN}"
export AE_APISHIM_SERVER="${SERVER}"
export AE_APISHIM_RUNTIME="${RUNTIME}"
export AE_APISHIM_DB="${SHIM_DB}"
export AE_APISHIM_TLS_CERT="${CERT}"
export AE_APISHIM_TLS_KEY="${KEY}"

python -m ae.controller --loop --specs specs/ --watch >/tmp/k1s-ctrl-spdy.log 2>&1 &
CTRL_PID=$!
python -m ae.apishim serve --host 127.0.0.1 --port "${PORT}" --token "${TOKEN}" --tls >/tmp/k1s-apishim-spdy.log 2>&1 &
SHIM_PID=$!

ready=0
for _i in $(seq 1 60); do
  if curl -fkSs -H "Authorization: Bearer ${TOKEN}" "${SERVER}/version" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "${SHIM_PID}" >/dev/null 2>&1; then
    echo "error: apishim exited before becoming ready"
    tail -n 200 /tmp/k1s-apishim-spdy.log || true
    exit 1
  fi
  sleep 0.5
done
if [[ "${ready}" != "1" ]]; then
  echo "error: apishim did not become ready on ${SERVER}"
  tail -n 200 /tmp/k1s-apishim-spdy.log || true
  tail -n 200 /tmp/k1s-ctrl-spdy.log || true
  exit 1
fi

scripts/dev/apishim_spdy_matrix.sh
