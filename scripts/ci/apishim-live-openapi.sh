#!/usr/bin/env bash
set -euo pipefail

# Live OpenAPI/compatibility spot-check.
# - If APISHIM_LIVE_KUBECONFIG is set, use that kubeconfig (e.g., dev lab or kind).
# - Else if APISHIM_KIND_CLUSTER is set and `kind` is installed, use the kind kubeconfig.
# - Otherwise start a local apishim backed by Postgres, generate a kubeconfig, and run checks.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKDIR="$(mktemp -d /tmp/apishim-live-XXXX)"
ARTIFACT_DIR="${APISHIM_LIVE_ARTIFACT_DIR:-/tmp/apishim-live}"
HOST="${APISHIM_LIVE_HOST:-127.0.0.1}"
PORT="${APISHIM_LIVE_PORT:-8445}"
TOKEN="${APISHIM_LIVE_TOKEN:-live-token}"
RUNTIME="${APISHIM_LIVE_RUNTIME:-stub}"
NAMESPACE="${APISHIM_LIVE_NAMESPACE:-apishim-live}"
KEEP_RESOURCES="${APISHIM_LIVE_KEEP_RESOURCES:-0}"
PYTHONPATH="${PYTHONPATH:-$ROOT_DIR/src}"
PG_PORT="${APISHIM_LIVE_PGPORT:-5432}"

mkdir -p "$ARTIFACT_DIR"

log() { echo "[live-openapi] $*"; }

cleanup() {
  local ec=$?
  if [[ -n "${SHIM_PID:-}" ]] && kill -0 "$SHIM_PID" 2>/dev/null; then
    kill "$SHIM_PID" >/dev/null 2>&1 || true
    wait "$SHIM_PID" 2>/dev/null || true
  fi
  if [[ -n "${PG_CONTAINER:-}" ]]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKDIR"
  exit $ec
}
trap cleanup EXIT

start_postgres() {
  if [[ -n "${AE_APISHIM_DSN:-}" ]]; then
    log "Using provided AE_APISHIM_DSN=$AE_APISHIM_DSN"
    return
  fi
  PG_CONTAINER="apishim-live-pg-${RANDOM}"
  log "Starting Postgres container ($PG_CONTAINER) on host port ${PG_PORT}"
  docker run -d --name "$PG_CONTAINER" \
    -e POSTGRES_USER=shim \
    -e POSTGRES_PASSWORD=shim \
    -e POSTGRES_DB=shim \
    -p "${PG_PORT}:5432" \
    postgres:15 >/dev/null

  for _ in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U shim -d shim >/dev/null 2>&1; then
      export AE_APISHIM_DSN="postgresql://shim:shim@localhost:${PG_PORT}/shim"
      export AE_STATE_DSN="$AE_APISHIM_DSN"
      log "Postgres is ready (dsn=$AE_APISHIM_DSN)"
      return
    fi
    sleep 1
  done
  log "Postgres failed to become ready"
  exit 1
}

start_shim() {
  log "Starting apishim on ${HOST}:${PORT} (runtime=${RUNTIME})"
  AE_APISHIM_ENABLE=1 \
    AE_APISHIM_TOKEN="$TOKEN" \
    AE_APISHIM_DSN="${AE_APISHIM_DSN:-}" \
    AE_STATE_DSN="${AE_STATE_DSN:-${AE_APISHIM_DSN:-}}" \
    AE_RUNTIME_BACKEND="$RUNTIME" \
    PYTHONPATH="$PYTHONPATH" \
    python -m ae.apishim serve \
    --host "$HOST" --port "$PORT" --token "$TOKEN" \
    >"$WORKDIR/shim.log" 2>&1 &
  SHIM_PID=$!
  sleep 4
}

generate_kubeconfig() {
  local target="$1"
  PYTHONPATH="$PYTHONPATH" \
    python -m ae.apishim kubeconfig \
    --server "http://${HOST}:${PORT}" \
    --token "$TOKEN" \
    --context apishim-live \
    --insecure-skip-tls-verify >"$target"
}

choose_kubeconfig() {
  if [[ -n "${APISHIM_LIVE_KUBECONFIG:-}" ]]; then
    export KUBECONFIG="$APISHIM_LIVE_KUBECONFIG"
    log "Using provided kubeconfig: $KUBECONFIG"
    return
  fi

  if [[ -n "${APISHIM_KIND_CLUSTER:-}" ]] && command -v kind >/dev/null 2>&1; then
    log "Using kind kubeconfig from cluster ${APISHIM_KIND_CLUSTER}"
    kind get kubeconfig --name "${APISHIM_KIND_CLUSTER}" >"$WORKDIR/kubeconfig"
    export KUBECONFIG="$WORKDIR/kubeconfig"
    return
  fi

  start_postgres
  start_shim
  generate_kubeconfig "$WORKDIR/kubeconfig"
  export KUBECONFIG="$WORKDIR/kubeconfig"
}

patch_manifest_namespace() {
  local src="$1"
  local dst="$2"
  sed "s/namespace: demo/namespace: ${NAMESPACE}/g" "$src" >"$dst"
}

main() {
  choose_kubeconfig
  log "Using kubeconfig context: $(kubectl config current-context 2>/dev/null || echo unknown)"
  log "Artifacts will be written to $ARTIFACT_DIR"

  mkdir -p "$ARTIFACT_DIR"

  log "Fetching OpenAPI from live endpoint"
  kubectl get --raw /openapi/v2 >"$ARTIFACT_DIR/openapi-v2.live.json"
  kubectl get --raw /openapi/v3 >"$ARTIFACT_DIR/openapi-v3.live.json"

  log "Validating fixtures against live OpenAPI"
  PYTHONPATH="$PYTHONPATH" python "$ROOT_DIR/scripts/validate-openapi-fixtures.py" \
    --spec "$ARTIFACT_DIR/openapi-v2.live.json" \
    | tee "$ARTIFACT_DIR/fixture-validate.log"

  CREATED_NS=0
  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    kubectl create namespace "$NAMESPACE" >/dev/null
    CREATED_NS=1
  fi

  WORK_MANIFEST="$WORKDIR/echo.yaml"
  patch_manifest_namespace "$ROOT_DIR/specs/examples/echo-k8s.yaml" "$WORK_MANIFEST"

  log "Server-side dry-run of sample manifest"
  kubectl apply --dry-run=server -f "$WORK_MANIFEST" -o yaml >"$ARTIFACT_DIR/echo-dry-run.yaml"

  log "Applying sample manifest and collecting live objects"
  kubectl apply -f "$WORK_MANIFEST" >/dev/null
  kubectl get deployment echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/deployment.live.yaml"
  kubectl get service echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/service.live.yaml"
  kubectl get hpa echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/hpa.live.yaml"
  kubectl get ingress echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/ingress.live.yaml" || true

  log "Running short watch for churn signals"
  kubectl get deployment -n "$NAMESPACE" --watch --timeout=10s -o name >"$ARTIFACT_DIR/watch.log" || true
  kubectl get events -n "$NAMESPACE" --field-selector involvedObject.name=echo -o wide >"$ARTIFACT_DIR/events.log" || true

  log "Helm render + server-side validation"
  HELM_TMP="$(mktemp -d "$WORKDIR/helm-XXXX")"
  helm create "$HELM_TMP/chart" >/dev/null
  helm template demo "$HELM_TMP/chart" -n "$NAMESPACE" >"$HELM_TMP/chart.yaml"
  kubectl apply --dry-run=server --validate=false -f "$HELM_TMP/chart.yaml" \
    >"$ARTIFACT_DIR/helm-dry-run.log"

  if [[ "$KEEP_RESOURCES" != "1" ]]; then
    log "Cleaning applied resources"
    kubectl delete -f "$WORK_MANIFEST" --ignore-not-found >/dev/null 2>&1 || true
    if [[ "$CREATED_NS" == "1" ]]; then
      kubectl delete namespace "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
    fi
  else
    log "KEEP_RESOURCES=1 set; skipping cleanup"
  fi

  log "Live OpenAPI validation complete"
}

main "$@"
