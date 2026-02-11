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

CONTEXTS=("current")
SUDO_UID=""
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  CONTEXTS+=("sudo-user")
  SUDO_UID="$(id -u "${SUDO_USER}" 2>/dev/null || true)"
fi

run_engine() {
  local ctx="$1"
  local bin="$2"
  shift 2
  if [[ "$ctx" == "sudo-user" && -n "${SUDO_USER:-}" ]]; then
    if [[ -n "${SUDO_UID}" ]]; then
      sudo -u "${SUDO_USER}" -H XDG_RUNTIME_DIR="/run/user/${SUDO_UID}" "$bin" "$@"
    else
      sudo -u "${SUDO_USER}" -H "$bin" "$@"
    fi
  else
    "$bin" "$@"
  fi
}

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

log "Stopping apishim"
if [[ -f state/apishim.pid ]]; then
  pid=$(cat state/apishim.pid || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
  fi
  rm -f state/apishim.pid || true
fi
pkill -f 'python.*-m ae\.apishim' 2>/dev/null || true

log "Stopping dev compose stack (caddy, prometheus)"
DEV_COMPOSE_FILES=(-f ops/dev/docker-compose.yaml)
if [[ -f ops/dev/docker-compose.cache.override.yml ]]; then
  DEV_COMPOSE_FILES+=(-f ops/dev/docker-compose.cache.override.yml)
fi
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    run_engine "$ctx" "$bin" compose "${DEV_COMPOSE_FILES[@]}" down >/dev/null 2>&1 || true
    for name in dev-registry-1 dev-caddy-1 dev-apishim-1 dev-prometheus-1 dev-haproxy; do
      if run_engine "$ctx" "$bin" ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$"; then
        run_engine "$ctx" "$bin" rm -f "$name" >/dev/null 2>&1 || true
      fi
    done
  done
done

log "Stopping dev NATS/etcd stack (nats-hub, nats-edge, etcd, postgres)"
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    run_engine "$ctx" "$bin" compose -f ops/dev/docker-compose.nats-etcd.yaml down >/dev/null 2>&1 || true
    extra_edges=$(run_engine "$ctx" "$bin" ps -aq --filter 'name=dev-nats-edge-' 2>/dev/null || true)
    if [[ -n "$extra_edges" ]]; then
      run_engine "$ctx" "$bin" rm -f $extra_edges >/dev/null 2>&1 || true
    fi
    for name in dev-nats-hub-1 dev-etcd-1 dev-postgres-1; do
      if run_engine "$ctx" "$bin" ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${name}$"; then
        run_engine "$ctx" "$bin" rm -f "$name" >/dev/null 2>&1 || true
      fi
    done
  done
done

log "Stopping k1s ingress containers (k1s-*)"
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    ids=""
    ids=$(run_engine "$ctx" "$bin" ps -aq 2>/dev/null | awk 'NF' || true)
    if [[ -n "$ids" ]]; then
      named=$(run_engine "$ctx" "$bin" ps -a --format '{{.ID}} {{.Names}}' 2>/dev/null | awk '$2 ~ /^k1s-/' | awk '{print $1}' || true)
      if [[ -n "$named" ]]; then
        run_engine "$ctx" "$bin" rm -f $named >/dev/null 2>&1 || true
      fi
    fi
  done
done

log "Stopping labs compose stacks (labs-aio, labs-compose)"
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    run_engine "$ctx" "$bin" compose -f ops/dev/labs-aio.yaml down >/dev/null 2>&1 || true
    run_engine "$ctx" "$bin" compose -f ops/dev/labs-compose.yaml down >/dev/null 2>&1 || true
  done
done

if command -v crictl >/dev/null 2>&1; then
  log "Stopping CRI pods/containers (k1s-managed)"
  label="app.kubernetes.io/managed-by=k1s"
  pods=$(crictl pods -q --label "$label" 2>/dev/null || true)
  if [[ -n "${pods}" ]]; then
    crictl stopp ${pods} >/dev/null 2>&1 || true
    crictl rmp ${pods} >/dev/null 2>&1 || true
  fi
  containers=$(crictl ps -a -q --label "$label" 2>/dev/null || true)
  if [[ -n "${containers}" ]]; then
    crictl stop ${containers} >/dev/null 2>&1 || true
    crictl rm ${containers} >/dev/null 2>&1 || true
  fi
fi

log "Removing demo app containers (label=ae.app or name=ae-*)"
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    ids_label=$(run_engine "$ctx" "$bin" ps -aq --filter 'label=ae.app' 2>/dev/null || true)
    ids_name=$(run_engine "$ctx" "$bin" ps -aq --filter 'name=^ae-' 2>/dev/null || true)
    ids=$(printf "%s\n%s\n" "$ids_label" "$ids_name" | awk 'NF' | sort -u)
    if [[ -n "${ids}" ]]; then
      run_engine "$ctx" "$bin" rm -f $ids >/dev/null 2>&1 || true
    fi
  done
done

log "Removing service proxy containers (name=ae-svc-*)"
for ctx in "${CONTEXTS[@]}"; do
  for bin in "${ENGINES[@]}"; do
    svc_ids=$(run_engine "$ctx" "$bin" ps -aq --filter 'name=ae-svc-' 2>/dev/null || true)
    if [[ -n "${svc_ids}" ]]; then
      run_engine "$ctx" "$bin" rm -f $svc_ids >/dev/null 2>&1 || true
    fi
  done
done

log "Clearing Labs shim artifacts (state/profiles/labs)"
rm -f state/profiles/labs/helm-demo.log state/profiles/labs/apishim.env >/dev/null 2>&1 || true

log "Cleanup complete"
