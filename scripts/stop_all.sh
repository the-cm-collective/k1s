#!/usr/bin/env bash
set -euo pipefail

log() { printf '\033[1;31m[stop-all]\033[0m %s\n' "$1"; }

# Determine container stack CLI
if command -v podman >/dev/null 2>&1; then
  BIN=podman
elif command -v docker >/dev/null 2>&1; then
  BIN=docker
else
  BIN=podman
fi

DOCS_PORT=${DOCS_PORT:-9109}
API_PORT=${API_PORT:-9108}

log "Stopping docs server"
if [[ -f state/docs_server.pid ]]; then
  pid=$(cat state/docs_server.pid || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
  fi
  rm -f state/docs_server.pid || true
fi
# Best-effort kill stray http.server processes bound to docs/site or :9109
pkill -f 'python.*http\.server.*--directory\s+docs/site' 2>/dev/null || true
pkill -f "python.*http\.server.*${DOCS_PORT}" 2>/dev/null || true

log "Stopping controller + supervisor"
if [[ -f state/controller.pid ]]; then
  pid=$(cat state/controller.pid || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
  fi
  rm -f state/controller.pid || true
fi
if [[ -f state/controller_supervisor.pid ]]; then
  pid=$(cat state/controller_supervisor.pid || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
  fi
  rm -f state/controller_supervisor.pid state/controller_supervisor.lock || true
fi
# Kill any strays on API port and matching patterns
pkill -f 'scripts/supervise_controller\.sh .*\s[0-9]{4,5}$' 2>/dev/null || true
pkill -f '\.venv-demo/bin/python -m ae\.controller --loop' 2>/dev/null || true
if ss -ltnp 2>/dev/null | awk '$4 ~ /:9108$/ {exit 0} END{exit 1}'; then
  pids=$(ss -ltnp 2>/dev/null | awk '$4 ~ /:9108$/ {print $NF}' | sed -E 's/.*pid=([0-9]+),.*/\1/')
  for p in $pids; do kill "$p" 2>/dev/null || true; done
fi

log "Stopping dev compose stack (caddy, prometheus)"
"$BIN" compose -f ops/dev/docker-compose.yaml down >/dev/null 2>&1 || true

log "Removing demo app containers (label=ae.app)"
ids=$("$BIN" ps -aq --filter 'label=ae.app' || true)
if [[ -n "${ids}" ]]; then
  "$BIN" rm -f $ids >/dev/null 2>&1 || true
fi

log "Cleanup complete"

