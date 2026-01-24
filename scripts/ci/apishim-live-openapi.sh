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
ALLOW_ANON="${APISHIM_LIVE_ALLOW_ANON:-1}"
PYTHONPATH="${PYTHONPATH:-$ROOT_DIR/src}"
PG_PORT="${APISHIM_LIVE_PGPORT:-5432}"
STARTED_SHIM=0

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
  ensure_runtime
  SHIM_ARGS=(--host "$HOST" --port "$PORT" --token "$TOKEN")
  if [[ "${ALLOW_ANON}" == "1" ]]; then
    SHIM_ARGS+=(--allow-anonymous)
  fi
  AE_APISHIM_ENABLE=1 \
    AE_APISHIM_TOKEN="$TOKEN" \
    AE_APISHIM_ALLOW_ANON="$ALLOW_ANON" \
    AE_APISHIM_DSN="${AE_APISHIM_DSN:-}" \
    AE_STATE_DSN="${AE_STATE_DSN:-${AE_APISHIM_DSN:-}}" \
    AE_RUNTIME_BACKEND="$RUNTIME" \
    PYTHONPATH="$PYTHONPATH" \
    python -m ae.apishim serve \
    "${SHIM_ARGS[@]}" \
    >"$WORKDIR/shim.log" 2>&1 &
  SHIM_PID=$!
  STARTED_SHIM=1
  sleep 4
}

ensure_runtime() {
  if [[ "$RUNTIME" == "stub" ]]; then
    return
  fi
  if [[ "$RUNTIME" == "docker" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
      log "docker runtime requested but docker CLI not available"
      exit 1
    fi
    docker info >/dev/null 2>&1 || {
      log "docker runtime requested but docker daemon is unavailable"
      exit 1
    }
    return
  fi
  if [[ "$RUNTIME" == "podman" ]]; then
    if ! command -v podman >/dev/null 2>&1; then
      log "podman runtime requested but podman CLI not available"
      exit 1
    fi
    podman info >/dev/null 2>&1 || {
      log "podman runtime requested but podman is unavailable"
      exit 1
    }
    return
  fi
  log "unsupported runtime '$RUNTIME' (expected stub|docker|podman)"
  exit 1
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
  if [[ "$STARTED_SHIM" == "1" ]]; then
    KCTL=(kubectl --server "http://${HOST}:${PORT}" --token "$TOKEN" --insecure-skip-tls-verify)
    DRYRUN_VALIDATE_FLAG=(--validate=false)
    APPLY_VALIDATE_FLAG=(--validate=false)
  else
    KCTL=(kubectl)
    DRYRUN_VALIDATE_FLAG=()
    APPLY_VALIDATE_FLAG=()
  fi
  log "Using kubeconfig context: $("${KCTL[@]}" config current-context 2>/dev/null || echo unknown)"
  log "Artifacts will be written to $ARTIFACT_DIR"

  mkdir -p "$ARTIFACT_DIR"

  log "Fetching OpenAPI from live endpoint"
  "${KCTL[@]}" get --raw /openapi/v2 >"$ARTIFACT_DIR/openapi-v2.live.json"
  "${KCTL[@]}" get --raw /openapi/v3 >"$ARTIFACT_DIR/openapi-v3.live.json"

  log "Validating fixtures against live OpenAPI"
  PYTHONPATH="$PYTHONPATH" python "$ROOT_DIR/scripts/validate-openapi-fixtures.py" \
    --spec "$ARTIFACT_DIR/openapi-v2.live.json" \
    | tee "$ARTIFACT_DIR/fixture-validate.log"

  CREATED_NS=0
  if ! "${KCTL[@]}" get namespace "$NAMESPACE" >/dev/null 2>&1; then
    "${KCTL[@]}" create namespace "$NAMESPACE" >/dev/null
    CREATED_NS=1
  fi

  WORK_MANIFESTS=()
  for src in \
    "$ROOT_DIR/specs/examples/echo-k8s.yaml" \
    "$ROOT_DIR/specs/examples/multi-replica-echo-k8s.yaml"; do
    dst="$WORKDIR/$(basename "$src")"
    patch_manifest_namespace "$src" "$dst"
    WORK_MANIFESTS+=("$dst")
  done

  log "Server-side dry-run of sample manifests"
  idx=0
  for manifest in "${WORK_MANIFESTS[@]}"; do
    idx=$((idx + 1))
    "${KCTL[@]}" apply --dry-run=server "${DRYRUN_VALIDATE_FLAG[@]}" -f "$manifest" -o yaml \
      >"$ARTIFACT_DIR/sample-${idx}-dry-run.yaml"
  done

  log "Applying sample manifests and collecting live objects"
  for manifest in "${WORK_MANIFESTS[@]}"; do
    "${KCTL[@]}" apply "${APPLY_VALIDATE_FLAG[@]}" -f "$manifest" >/dev/null
  done
  "${KCTL[@]}" get deployment echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/deployment.live.yaml"
  "${KCTL[@]}" get service echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/service.live.yaml"
  "${KCTL[@]}" get hpa echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/hpa.live.yaml"
  "${KCTL[@]}" get ingress echo -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/ingress.live.yaml" || true
  "${KCTL[@]}" get pdb -n "$NAMESPACE" -o yaml >"$ARTIFACT_DIR/pdb.live.yaml" || true

  if [[ "$STARTED_SHIM" == "1" && "$RUNTIME" != "stub" ]]; then
    log "Non-stub runtime checks (logs/exec)"
    EXEC_MANIFEST_RAW="$WORKDIR/exec-smoke.yaml"
    EXEC_MANIFEST="$WORKDIR/exec-smoke-ns.yaml"
    cat >"$EXEC_MANIFEST_RAW" <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: exec-smoke
  namespace: demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app: exec-smoke
  template:
    metadata:
      labels:
        app: exec-smoke
    spec:
      containers:
      - name: exec-smoke
        image: busybox:1.36
        command: ["sleep", "300"]
EOF
    patch_manifest_namespace "$EXEC_MANIFEST_RAW" "$EXEC_MANIFEST"
    "${KCTL[@]}" apply "${APPLY_VALIDATE_FLAG[@]}" -f "$EXEC_MANIFEST" >/dev/null
    POD_NAME=""
    for _ in $(seq 1 30); do
      POD_NAME="$("${KCTL[@]}" get pods -n "$NAMESPACE" -l app=exec-smoke -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
      if [[ -n "$POD_NAME" ]]; then
        break
      fi
      sleep 2
    done
    if [[ -z "$POD_NAME" ]]; then
      "${KCTL[@]}" get pods -n "$NAMESPACE" -l app=exec-smoke -o wide || true
      log "exec-smoke pod was not created"
      exit 1
    fi
    if ! "${KCTL[@]}" wait --for=condition=Ready pod "$POD_NAME" -n "$NAMESPACE" --timeout=120s >/dev/null 2>&1; then
      "${KCTL[@]}" get pods -n "$NAMESPACE" -l app=exec-smoke -o wide || true
      log "exec-smoke pod failed to become ready"
      exit 1
    fi
    "${KCTL[@]}" logs -n "$NAMESPACE" "$POD_NAME" >/dev/null
    if ! "${KCTL[@]}" --request-timeout=15s exec -n "$NAMESPACE" "$POD_NAME" -- /bin/sh -c "echo exec-ok" >/dev/null; then
      log "kubectl exec failed; falling back to JSON exec endpoint"
      if ! curl -sS \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: application/json" \
        -X POST \
        --data '{"command":["/bin/sh","-c","echo exec-ok"]}' \
        "http://${HOST}:${PORT}/api/v1/namespaces/${NAMESPACE}/pods/${POD_NAME}/exec" \
        >/dev/null; then
        log "exec fallback failed"
        exit 1
      fi
    fi
  fi

  log "Running short watch for churn signals"
  "${KCTL[@]}" get deployment -n "$NAMESPACE" --watch --request-timeout=10s -o name \
    >"$ARTIFACT_DIR/watch.log" || true
  "${KCTL[@]}" get events -n "$NAMESPACE" --field-selector involvedObject.name=echo -o wide \
    >"$ARTIFACT_DIR/events.log" || true

  log "Helm render + server-side validation"
  HELM_TMP="$(mktemp -d "$WORKDIR/helm-XXXX")"
  helm create "$HELM_TMP/chart" >/dev/null
  helm template demo "$HELM_TMP/chart" -n "$NAMESPACE" --skip-tests >"$HELM_TMP/chart.yaml"
  "${KCTL[@]}" apply --dry-run=server --validate=false -f "$HELM_TMP/chart.yaml" \
    >"$ARTIFACT_DIR/helm-dry-run.log"

  if [[ "$KEEP_RESOURCES" != "1" ]]; then
    log "Cleaning applied resources"
    for manifest in "${WORK_MANIFESTS[@]}"; do
      "${KCTL[@]}" delete -f "$manifest" --ignore-not-found --wait=false --request-timeout=15s \
        >/dev/null 2>&1 || true
    done
    if [[ -n "${EXEC_MANIFEST:-}" ]]; then
      "${KCTL[@]}" delete -f "$EXEC_MANIFEST" --ignore-not-found --wait=false --request-timeout=15s \
        >/dev/null 2>&1 || true
    fi
    if [[ "$CREATED_NS" == "1" ]]; then
      "${KCTL[@]}" delete namespace "$NAMESPACE" --ignore-not-found --wait=false --request-timeout=15s \
        >/dev/null 2>&1 || true
    fi
  else
    log "KEEP_RESOURCES=1 set; skipping cleanup"
  fi

  log "Live OpenAPI validation complete"
}

main "$@"
