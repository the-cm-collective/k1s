#!/usr/bin/env bash
set -euo pipefail

log() { printf '\033[1;31m[stop-all]\033[0m %s\n' "$1"; }

# Determine container engines present (prefer cleaning both if available)
ENGINES=()
for bin in podman docker; do
  if command -v "$bin" >/dev/null 2>&1; then
    ENGINES+=("$bin")
  fi
done
if [[ ${#ENGINES[@]} -eq 0 ]]; then
  ENGINES=(podman)
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
pkill -f 'scripts/supervise_controller\.sh' 2>/dev/null || true
pkill -f 'python.*-m ae\.controller --loop' 2>/dev/null || true
if ss -ltnp 2>/dev/null | awk '$4 ~ /:9108$/ {exit 0} END{exit 1}'; then
  pids=$(ss -ltnp 2>/dev/null | awk '$4 ~ /:9108$/ {print $NF}' | sed -E 's/.*pid=([0-9]+),.*/\1/')
  for p in $pids; do kill "$p" 2>/dev/null || true; done
fi

log "Stopping dev compose stack (caddy, prometheus)"
DEV_COMPOSE_FILES=(-f ops/dev/docker-compose.yaml)
if [[ -f ops/dev/docker-compose.cache.override.yml ]]; then
  DEV_COMPOSE_FILES+=(-f ops/dev/docker-compose.cache.override.yml)
fi
for bin in "${ENGINES[@]}"; do
  "$bin" compose "${DEV_COMPOSE_FILES[@]}" down >/dev/null 2>&1 || true
  if "$bin" ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^dev-registry-1$'; then
    "$bin" rm -f dev-registry-1 >/dev/null 2>&1 || true
  fi
done

log "Stopping labs compose stacks (labs-aio, labs-compose)"
for bin in "${ENGINES[@]}"; do
  "$bin" compose -f ops/dev/labs-aio.yaml down >/dev/null 2>&1 || true
  "$bin" compose -f ops/dev/labs-compose.yaml down >/dev/null 2>&1 || true
done

log "Removing demo app containers (label=ae.app or name=ae-*)"
for bin in "${ENGINES[@]}"; do
  ids_label=$("$bin" ps -aq --filter 'label=ae.app' 2>/dev/null || true)
  ids_name=$("$bin" ps -aq --filter 'name=^ae-' 2>/dev/null || true)
  ids=$(printf "%s\n%s\n" "$ids_label" "$ids_name" | awk 'NF' | sort -u)
  if [[ -n "${ids}" ]]; then
    "$bin" rm -f $ids >/dev/null 2>&1 || true
  fi
done

log "Removing service proxy containers (name=ae-svc-*)"
for bin in "${ENGINES[@]}"; do
  svc_ids=$("$bin" ps -aq --filter 'name=ae-svc-' 2>/dev/null || true)
  if [[ -n "${svc_ids}" ]]; then
    "$bin" rm -f $svc_ids >/dev/null 2>&1 || true
  fi
done

log "Clearing Labs shim artifacts (state/labs)"
rm -f state/labs/helm-demo.log state/labs/apishim.env >/dev/null 2>&1 || true

log "Cleanup complete"
