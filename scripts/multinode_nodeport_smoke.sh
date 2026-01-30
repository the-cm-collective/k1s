#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)
cd "$ROOT_DIR"

log() {
  printf '[mn-nodeport] %s\n' "$1"
}

APP_NAME="${AE_MN_NODEPORT_APP:-mn-nodeport-echo}"
IMAGE="${AE_MN_NODEPORT_IMAGE:-mendhak/http-https-echo:37}"
NODEPORT="${AE_MN_NODEPORT_PORT:-32080}"
SERVICE_PORT="${AE_MN_NODEPORT_SERVICE_PORT:-8080}"
TARGET_PORT="${AE_MN_NODEPORT_TARGET_PORT:-8080}"
HOST="${AE_MN_NODEPORT_HOST:-${AE_NODEPORT_HOST:-}}"
PATH_URL="${AE_MN_NODEPORT_PATH:-/healthz}"
NAMESPACE="${AE_MN_NODEPORT_NAMESPACE:-default}"
ATTEMPTS="${AE_MN_NODEPORT_ATTEMPTS:-30}"
INTERVAL="${AE_MN_NODEPORT_INTERVAL:-2}"
CLEANUP="${AE_MN_NODEPORT_CLEANUP:-1}"

SERVER="${AE_SERVER:-}"
TOKEN="${AE_TOKEN:-${AE_API_ADMIN_TOKEN:-${AE_API_READ_TOKEN:-}}}"

if [[ -z "${HOST}" ]]; then
  log "missing host IP; set AE_MN_NODEPORT_HOST (worker/host IP to test NodePort)"
  exit 1
fi

spec_dir="$(mktemp -d)"
manifest="${spec_dir}/${APP_NAME}-deployment.yaml"

cat <<EOF_MANIFEST > "${manifest}"
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  image: ${IMAGE}
  replicas: 1
  ports:
    - name: http
      containerPort: ${TARGET_PORT}
  service:
    type: NodePort
    ports:
      - name: http
        port: ${SERVICE_PORT}
        targetPort: ${TARGET_PORT}
        nodePort: ${NODEPORT}
  health:
    readiness:
      httpGet:
        path: ${PATH_URL}
        port: ${TARGET_PORT}
      initialDelaySeconds: 1
      timeoutSeconds: 1
      periodSeconds: 2
      failureThreshold: 20
EOF_MANIFEST

cleanup() {
  if [[ "${CLEANUP}" == "1" ]]; then
    log "cleanup: deleting ${APP_NAME}"
    if [[ -n "${SERVER}" ]]; then
      python -m ae.cli --server "${SERVER}" ${TOKEN:+--token "${TOKEN}"} \
        delete "${APP_NAME}" --namespace "${NAMESPACE}" >/dev/null 2>&1 || true
    else
      python -m ae.cli delete "${APP_NAME}" --namespace "${NAMESPACE}" >/dev/null 2>&1 || true
    fi
  fi
  rm -rf "${spec_dir}"
}
trap cleanup EXIT

log "app=${APP_NAME} nodePort=${NODEPORT} host=${HOST} image=${IMAGE}"
log "note: agent service proxy must be enabled on each node (AE_AGENT_SERVICE_PROXY=1)"

if [[ -n "${SERVER}" ]]; then
  log "applying via remote API: ${SERVER}"
  python -m ae.cli --server "${SERVER}" ${TOKEN:+--token "${TOKEN}"} \
    apply -f "${manifest}" --namespace "${NAMESPACE}" >/dev/null
else
  log "applying locally"
  python -m ae.cli apply -f "${manifest}" --namespace "${NAMESPACE}" >/dev/null
fi

for i in $(seq 1 "${ATTEMPTS}"); do
  if curl -fsS "http://${HOST}:${NODEPORT}${PATH_URL}" >/dev/null 2>&1; then
    log "NodePort OK"
    exit 0
  fi
  log "attempt ${i}/${ATTEMPTS} failed; retrying in ${INTERVAL}s"
  sleep "${INTERVAL}"
done

log "NodePort did not respond after ${ATTEMPTS} attempts"
exit 1
