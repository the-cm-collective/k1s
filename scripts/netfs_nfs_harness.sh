#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[netfs-harness] %s\n' "$1"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 2
  fi
}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
STATE_ROOT=${STATE_ROOT:-"$ROOT_DIR/state"}
mkdir -p "$STATE_ROOT"

HARNESS_DIR=${HARNESS_DIR:-"$(mktemp -d "$STATE_ROOT/netfs-harness-XXXXXX")"}
APISHIM_DB=${APISHIM_DB:-"$HARNESS_DIR/apishim.db"}
STATE_DB=${STATE_DB:-"$HARNESS_DIR/controller.db"}
NETFS_ROOT=${NETFS_ROOT:-"$HARNESS_DIR/netfs-root"}
SC_FILE=${SC_FILE:-"$HARNESS_DIR/storage-classes.yaml"}
KEEP_STATE=${KEEP_STATE:-0}
SKIP_MOUNT_PREFLIGHT=${SKIP_MOUNT_PREFLIGHT:-0}

APISHIM_HOST=${APISHIM_HOST:-127.0.0.1}
APISHIM_PORT=${APISHIM_PORT:-18445}
APISHIM_URL="http://${APISHIM_HOST}:${APISHIM_PORT}"

AGENT_HOST=${AGENT_HOST:-127.0.0.1}
AGENT_PORT=${AGENT_PORT:-18100}
AGENT_URL="http://${AGENT_HOST}:${AGENT_PORT}"
NODE_ID=${NODE_ID:-netfs-node}

NFS_CONTAINER=${NFS_CONTAINER:-ae-netfs-nfs-harness}
NFS_EXPORT_DIR=${NFS_EXPORT_DIR:-"$HARNESS_DIR/exports"}
NFS_SERVER=${NFS_SERVER:-127.0.0.1}
# The nfs-server-alpine image exports /exports as the NFSv4 pseudo-root (fsid=0),
# so clients should mount subpaths relative to that root (e.g., /netfs).
NFS_PATH=${NFS_PATH:-/netfs}

PV_NAME=${PV_NAME:-netfs-pv-harness}
PVC_NAME=${PVC_NAME:-netfs-pvc-harness}
DEPLOY_NAME=${DEPLOY_NAME:-netfs-echo-harness}
HARNESS_MODE=${NETFS_HARNESS_MODE:-smoke}
CLONE_PVC_NAME=${NETFS_CLONE_PVC_NAME:-"${PVC_NAME}-clone"}
MOUNT_PATH="${NETFS_ROOT}/default/${PVC_NAME}"
CLONE_MOUNT_PATH="${NETFS_ROOT}/default/${CLONE_PVC_NAME}"

APISHIM_PID=""
AGENT_PID=""

kill_quick() {
  local pid=$1
  if [[ -z "${pid}" ]]; then
    return 0
  fi
  kill "${pid}" >/dev/null 2>&1 || true
  sleep 0.5
  kill -KILL "${pid}" >/dev/null 2>&1 || true
}

umount_if_mounted() {
  local path=$1
  if [[ -z "${path}" || ! -d "${path}" ]]; then
    return 0
  fi
  if ! grep -qs " ${path} " /proc/mounts; then
    return 0
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    return 0
  fi
  timeout 15 umount -l -f "${path}" >/dev/null 2>&1 || true
}

cleanup() {
  set +e
  log "cleaning up"
  if [[ -n "${AGENT_PID}" ]]; then
    kill_quick "${AGENT_PID}"
  fi
  if [[ -n "${APISHIM_PID}" ]]; then
    kill_quick "${APISHIM_PID}"
  fi
  umount_if_mounted "${MOUNT_PATH}"
  umount_if_mounted "${CLONE_MOUNT_PATH}"
  timeout 20 docker rm -f "${NFS_CONTAINER}" >/dev/null 2>&1 || true
  if [[ "${KEEP_STATE}" == "1" ]]; then
    log "preserving harness dir: ${HARNESS_DIR}"
  else
    rm -rf "${HARNESS_DIR}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

need_cmd docker
need_cmd curl
need_cmd python
if [[ "${HARNESS_MODE}" != "csi" ]]; then
  need_cmd rpcinfo
fi

log "harness dir: ${HARNESS_DIR}"
mkdir -p "${NETFS_ROOT}"
if [[ "${HARNESS_MODE}" != "csi" ]]; then
  mkdir -p "${NFS_EXPORT_DIR}/netfs"
fi

cat >"${SC_FILE}" <<EOF_SC
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: k1s-nfs
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: k1s.io/nfs
parameters:
  server: ${NFS_SERVER}
  path: ${NFS_PATH}
  hostPath: ${NFS_EXPORT_DIR}/netfs
reclaimPolicy: Retain
volumeBindingMode: Immediate
allowVolumeExpansion: false
mountOptions:
  - vers=4.2
EOF_SC

if [[ "${HARNESS_MODE}" != "csi" ]]; then
  log "starting NFS server container: ${NFS_CONTAINER}"
  docker rm -f "${NFS_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${NFS_CONTAINER}" \
    --privileged \
    --network host \
    -e SHARED_DIRECTORY=/exports \
    -e PERMITTED=127.0.0.1 \
    -v "${NFS_EXPORT_DIR}:/exports" \
    itsthenetwork/nfs-server-alpine:latest >/dev/null

  log "waiting for NFS rpcbind/nfsd"
  for _ in $(seq 1 30); do
    if rpcinfo -p "${NFS_SERVER}" 2>/dev/null | grep -q "100003"; then
      break
    fi
    sleep 1
  done
  if ! rpcinfo -p "${NFS_SERVER}" 2>/dev/null | grep -q "100003"; then
    echo "NFS server did not become ready" >&2
    docker logs "${NFS_CONTAINER}" | tail -n 60 >&2 || true
    exit 1
  fi

  if [[ "${SKIP_MOUNT_PREFLIGHT}" != "1" && "${EUID}" -ne 0 ]]; then
    log "running non-root NFS mount preflight"
    test_mount="${HARNESS_DIR}/mount-preflight"
    test_err="${HARNESS_DIR}/mount-preflight.err"
    mkdir -p "${test_mount}"
    set +e
    timeout 15 mount -t nfs -o vers=4.2,ro "${NFS_SERVER}:${NFS_PATH}" "${test_mount}" \
      2>"${test_err}"
    rc=$?
    set -e
    if [[ "${rc}" -ne 0 ]]; then
      echo "non-root NFS mount preflight failed" >&2
      cat "${test_err}" >&2 || true
      echo "run with sudo (or set SKIP_MOUNT_PREFLIGHT=1 to continue anyway)" >&2
      exit 3
    fi
    umount "${test_mount}" >/dev/null 2>&1 || true
  fi
fi

log "starting apishim on ${APISHIM_URL}"
AE_APISHIM_ENABLE=1 \
AE_APISHIM_ALLOW_ANON=1 \
AE_APISHIM_DB="${APISHIM_DB}" \
AE_STATE_DB="${STATE_DB}" \
AE_STORAGE_PROVISIONERS="${SC_FILE}" \
AE_APISHIM_RUNTIME=docker \
python -m ae.apishim serve \
  --host "${APISHIM_HOST}" \
  --port "${APISHIM_PORT}" \
  --allow-anon \
  >"${HARNESS_DIR}/apishim.log" 2>&1 &
APISHIM_PID=$!

for _ in $(seq 1 30); do
  if curl -fsS "${APISHIM_URL}/api" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "${APISHIM_URL}/api" >/dev/null

log "starting node agent on ${AGENT_URL}"
AE_ENABLE_NETFS=1 \
AE_APISHIM_DB="${APISHIM_DB}" \
AE_NETFS_ROOT="${NETFS_ROOT}" \
AE_NODE_ID="${NODE_ID}" \
AE_NODE_NAME="${NODE_ID}" \
python -m ae.node.server \
  --host "${AGENT_HOST}" \
  --port "${AGENT_PORT}" \
  >"${HARNESS_DIR}/agent.log" 2>&1 &
AGENT_PID=$!

for _ in $(seq 1 30); do
  if curl -fsS "${AGENT_URL}/v1/containers" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "${AGENT_URL}/v1/containers" >/dev/null

log "registering node ${NODE_ID} -> ${AGENT_URL}"
python - <<PY
from pathlib import Path
from ae.controller.state import SQLiteStateStore

store = SQLiteStateStore(Path("${STATE_DB}"))
store.upsert_node("${NODE_ID}", name="${NODE_ID}", endpoint="${AGENT_URL}")
store.record_heartbeat("${NODE_ID}", "Ready")
PY

if [[ "${HARNESS_MODE}" == "snapshot" ]]; then
  log "running NetFS snapshot/clone smoke test"
  APISHIM_URL="${APISHIM_URL}" \
  NETFS_STORAGE_CLASS=k1s-nfs \
  NETFS_ROOT="${NETFS_ROOT}" \
  NETFS_SRC_PVC_NAME="${PVC_NAME}" \
  NETFS_CLONE_PVC_NAME="${CLONE_PVC_NAME}" \
  NETFS_SRC_DEPLOYMENT_NAME="${DEPLOY_NAME}-src" \
  NETFS_CLONE_DEPLOYMENT_NAME="${DEPLOY_NAME}-clone" \
  "${ROOT_DIR}/scripts/netfs_snapshot_clone.sh"
elif [[ "${HARNESS_MODE}" == "csi" ]]; then
  log "running NetFS CSI smoke test"
  APISHIM_URL="${APISHIM_URL}" \
  NETFS_STORAGE_CLASS=k1s-csi \
  NETFS_ROOT="${NETFS_ROOT}" \
  NETFS_PV_NAME="${PV_NAME}" \
  NETFS_PVC_NAME="${PVC_NAME}" \
  NETFS_DEPLOYMENT_NAME="${DEPLOY_NAME}" \
  NETFS_NODE_ID="${NODE_ID}" \
  "${ROOT_DIR}/scripts/netfs_csi_smoke.sh"
else
  log "running NetFS smoke test"
  APISHIM_URL="${APISHIM_URL}" \
  NETFS_STORAGE_CLASS=k1s-nfs \
  NETFS_PV_NAME="${PV_NAME}" \
  NETFS_PVC_NAME="${PVC_NAME}" \
  NETFS_DEPLOYMENT_NAME="${DEPLOY_NAME}" \
  NETFS_DYNAMIC=1 \
  NFS_SERVER="${NFS_SERVER}" \
  NFS_PATH="${NFS_PATH}" \
  "${ROOT_DIR}/scripts/netfs_smoke.sh"
fi

if [[ "${HARNESS_MODE}" != "csi" ]]; then
  mount_targets=("${MOUNT_PATH}")
  if [[ "${HARNESS_MODE}" == "snapshot" ]]; then
    mount_targets+=("${CLONE_MOUNT_PATH}")
  fi

  for target in "${mount_targets[@]}"; do
    log "waiting for NFS mount at ${target}"
    mounted=0
    for _ in $(seq 1 45); do
      if grep -qs " ${target} " /proc/mounts; then
        mounted=1
        break
      fi
      sleep 1
    done

    if [[ "${mounted}" != "1" ]]; then
      echo "mount not detected at ${target}" >&2
      log "recent events"
      events_json=$(curl -fsS "${APISHIM_URL}/api/v1/namespaces/default/events" || true)
      if [[ -z "${events_json}" ]]; then
        log "no events returned from apishim"
      else
        python - "${events_json}" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
if not raw.strip():
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items", [])
for ev in items[-20:]:
    involved = (ev.get("involvedObject") or {}).get("kind", "")
    if involved != "PersistentVolumeClaim":
        continue
    name = (ev.get("involvedObject") or {}).get("name", "")
    reason = ev.get("reason", "")
    msg = ev.get("message", "")
    print(f"PVC event: {name} {reason} - {msg}")
PY
      fi
      exit 1
    fi
  done

  log "mount detected"
fi
