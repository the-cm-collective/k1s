#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)
cd "$ROOT_DIR"

log() {
  printf '[cri-nodeport] %s\n' "$1"
}

APP_NAME="${AE_CRI_NODEPORT_APP:-cri-nodeport-echo}"
IMAGE="${AE_CRI_NODEPORT_IMAGE:-mendhak/http-https-echo:37}"
NODEPORT="${AE_CRI_NODEPORT_PORT:-32080}"
SERVICE_PORT="${AE_CRI_NODEPORT_SERVICE_PORT:-8080}"
TARGET_PORT="${AE_CRI_NODEPORT_TARGET_PORT:-8080}"
HOST="${AE_CRI_NODEPORT_HOST:-${AE_NODE_ADVERTISE_IP:-127.0.0.1}}"
PATH_URL="${AE_CRI_NODEPORT_PATH:-/healthz}"
ATTEMPTS="${AE_CRI_NODEPORT_ATTEMPTS:-20}"
INTERVAL="${AE_CRI_NODEPORT_INTERVAL:-1}"
CLEANUP="${AE_CRI_NODEPORT_CLEANUP:-1}"
STATE_DB="${AE_STATE_DB:-}"

iptables_bin="${AE_IPTABLES_BIN:-iptables}"
if ! command -v "${iptables_bin}" >/dev/null 2>&1; then
  log "iptables not found (AE_IPTABLES_BIN=${iptables_bin})"
  exit 1
fi
if [[ "${EUID}" -ne 0 ]]; then
  log "root required to manage iptables (run with sudo)"
  exit 1
fi

cleanup_state=0
if [[ -z "${STATE_DB}" ]]; then
  STATE_DB="$(mktemp -p /tmp ae-cri-nodeport-XXXX.db)"
  cleanup_state=1
fi

tmp_dir="$(mktemp -d)"
spec_dir="${tmp_dir}/specs"
mkdir -p "${spec_dir}"

cat <<EOF > "${spec_dir}/${APP_NAME}-deployment.yaml"
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: default
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
      failureThreshold: 15
EOF

cleanup() {
  if [[ "${CLEANUP}" == "1" ]]; then
    log "cleanup: removing workload and service rules"
    AE_STATE_DB="${STATE_DB}" AE_RUNTIME_BACKEND=cri \
      python -m ae.cli delete "${APP_NAME}" --namespace default >/dev/null 2>&1 || true
    APP_NAME="${APP_NAME}" AE_STATE_DB="${STATE_DB}" AE_IPTABLES_BIN="${iptables_bin}" \
      python - <<'PY' || true
import os
from pathlib import Path

from ae.controller.state import SQLiteStateStore
from ae.network import IptablesProvider

app = os.environ.get("APP_NAME", "")
db_path = Path(os.environ.get("AE_STATE_DB", "state/controller.db"))
if app and db_path.exists():
    store = SQLiteStateStore(db_path)
    provider = IptablesProvider(
        store,
        iptables_bin=os.environ.get("AE_IPTABLES_BIN", "iptables"),
    )
    try:
        provider.remove_service(app)
    except Exception:
        pass
    try:
        store.delete_service(app)
    except Exception:
        pass
PY
  fi
  rm -rf "${tmp_dir}"
  if [[ "${cleanup_state}" == "1" ]]; then
    rm -f "${STATE_DB}"
  fi
}
trap cleanup EXIT

log "app=${APP_NAME} nodePort=${NODEPORT} host=${HOST} image=${IMAGE}"
log "reconciling via CRI (service proxy: iptables)"

for i in $(seq 1 "${ATTEMPTS}"); do
  AE_STATE_DB="${STATE_DB}" \
  AE_RUNTIME_BACKEND=cri \
  AE_ENABLE_SERVICE_PROXY=1 \
  AE_SERVICE_PROVIDER=iptables \
    python -m ae.controller --once --specs "${spec_dir}" >/dev/null 2>&1 || true

  NODEPORT_HOST="${HOST}" NODEPORT_PORT="${NODEPORT}" NODEPORT_PATH="${PATH_URL}" \
    python - <<'PY' && { log "NodePort OK"; exit 0; }
import os
import sys
import urllib.request

host = os.environ.get("NODEPORT_HOST", "127.0.0.1")
port = os.environ.get("NODEPORT_PORT", "32080")
path = os.environ.get("NODEPORT_PATH", "/healthz")
url = f"http://{host}:{port}{path}"
try:
    with urllib.request.urlopen(url, timeout=1) as resp:
        code = resp.getcode()
        if 200 <= code < 400:
            sys.exit(0)
except Exception:
    sys.exit(1)
sys.exit(1)
PY

  log "attempt ${i}/${ATTEMPTS} failed; retrying in ${INTERVAL}s"
  sleep "${INTERVAL}"
done

log "NodePort did not respond after ${ATTEMPTS} attempts"
exit 1
