#!/usr/bin/env bash
set -euo pipefail

# Simple end-to-end smoke test for the multi-port example.
# Prereqs: init_demo.sh, Docker/Podman, sops. Uses default Caddy HTTPS port 8443 unless overridden.

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &>/dev/null && pwd)
cd "$ROOT_DIR"

export CADDY_HTTPS_PORT="${CADDY_HTTPS_PORT:-8443}"

echo "[e2e] Starting demo with multi-port example"
./scripts/init_demo.sh --demo-echo-multi -y

echo "[e2e] Checking controller status for echo-multi"
python -m ae.cli status echo-multi

if ! python -m ae.cli status echo-multi | grep -q 'ready=1'; then
  echo "[e2e] ERROR: echo-multi not ready"
  exit 1
fi

echo "[e2e] Curling ingress at https://echo-multi.home.arpa:${CADDY_HTTPS_PORT}/"
code=$(curl -ksS -o /dev/null -w '%{http_code}' "https://echo-multi.home.arpa:${CADDY_HTTPS_PORT}/" || true)
if [[ "$code" != "200" && "$code" != "404" ]]; then
  echo "[e2e] ERROR: unexpected HTTP status: $code"
  exit 1
fi

echo "[e2e] OK: multi-port E2E passed (status=$code)"
