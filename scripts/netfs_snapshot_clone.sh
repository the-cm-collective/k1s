#!/usr/bin/env bash
set -euo pipefail

APISHIM_URL=${APISHIM_URL:-http://127.0.0.1:8445}
TOKEN=${AE_APISHIM_TOKEN:-}
NS=${NETFS_NAMESPACE:-default}
SC=${NETFS_STORAGE_CLASS:-k1s-nfs}
NETFS_ROOT=${NETFS_ROOT:-/var/lib/ae/netfs}
PYTHON_BIN=${PYTHON_BIN:-python}
SYNC_TIMEOUT=${NETFS_SYNC_TIMEOUT:-10}
SYNC_SKIP=${NETFS_SYNC_SKIP:-0}

SRC_PVC=${NETFS_SRC_PVC_NAME:-netfs-pvc-src}
CLONE_PVC=${NETFS_CLONE_PVC_NAME:-netfs-pvc-clone}
SRC_DEPLOY=${NETFS_SRC_DEPLOYMENT_NAME:-netfs-src}
CLONE_DEPLOY=${NETFS_CLONE_DEPLOYMENT_NAME:-netfs-clone}

SNAP_CLASS=${NETFS_SNAPSHOT_CLASS_NAME:-k1s-nfs-snap}
SNAP_NAME=${NETFS_SNAPSHOT_NAME:-netfs-snap}

SRC_MOUNT="${NETFS_ROOT}/${NS}/${SRC_PVC}"
CLONE_MOUNT="${NETFS_ROOT}/${NS}/${CLONE_PVC}"

SRC_PV=""
CLONE_PV=""
SNAP_CONTENT=""

log() {
  printf '[netfs-snapshot] %s\n' "$1"
}

auth_args=()
if [[ -n "$TOKEN" ]]; then
  auth_args=(-H "Authorization: Bearer ${TOKEN}")
fi

req() {
  local method=$1
  local url=$2
  shift 2
  curl -sS "${auth_args[@]}" -H "Content-Type: application/json" -X "$method" "$url" "$@"
}

put_json() {
  local url=$1
  req PUT "$url" --data @- >/dev/null
}

delete_if_exists() {
  local url=$1
  req DELETE "$url" >/dev/null 2>&1 || true
}

flush_data() {
  local file_path=$1
  if [[ "${SYNC_SKIP}" == "1" ]]; then
    return 0
  fi
  if command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    if command -v timeout >/dev/null 2>&1; then
      timeout "${SYNC_TIMEOUT}" "${PYTHON_BIN}" - "${file_path}" <<'PY' || true
import os
import sys

target = sys.argv[1] if len(sys.argv) > 1 else ""
if target:
    with open(target, "rb") as fh:
        os.fsync(fh.fileno())
PY
      return 0
    fi
    "${PYTHON_BIN}" - "${file_path}" <<'PY' || true
import os
import sys

target = sys.argv[1] if len(sys.argv) > 1 else ""
if target:
    with open(target, "rb") as fh:
        os.fsync(fh.fileno())
PY
    return 0
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout "${SYNC_TIMEOUT}" sync || true
  else
    sync || true
  fi
}

dump_recent_events() {
  log "recent events"
  set +e
  local events_json tmp_events
  events_json=$(curl -fsS "${auth_args[@]}" "${APISHIM_URL}/api/v1/namespaces/${NS}/events" || true)
  if [[ -z "${events_json}" ]]; then
    log "no events returned from apishim"
    set -e
    return 0
  fi
  tmp_events=$(mktemp)
  printf '%s' "${events_json}" >"${tmp_events}"
  "${PYTHON_BIN}" - "${tmp_events}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read()
if not raw.strip():
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items", [])
for ev in items[-25:]:
    involved = ev.get("involvedObject") or {}
    kind = involved.get("kind", "")
    name = involved.get("name", "")
    reason = ev.get("reason", "")
    msg = ev.get("message", "")
    if kind in {"PersistentVolumeClaim", "VolumeSnapshot"}:
        print(f"{kind} {name} {reason} - {msg}")
PY
  rm -f "${tmp_events}"
  set -e
}

dump_storage_state() {
  log "PVC ${SRC_PVC} status"
  set +e
  local tmp_pvc tmp_pvs tmp_sc
  tmp_pvc=$(mktemp)
  req GET "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${SRC_PVC}" >"${tmp_pvc}"
  "${PYTHON_BIN}" - "${tmp_pvc}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read()
if not raw.strip():
    raise SystemExit(0)
data = json.loads(raw)
spec = data.get("spec") or {}
status = data.get("status") or {}
meta = data.get("metadata") or {}
print(f"name={meta.get('name')} uid={meta.get('uid')} phase={status.get('phase')}")
print("spec:", spec)
print("status:", status)
PY
  rm -f "${tmp_pvc}"

  log "PV list"
  tmp_pvs=$(mktemp)
  req GET "${APISHIM_URL}/api/v1/persistentvolumes" >"${tmp_pvs}"
  "${PYTHON_BIN}" - "${tmp_pvs}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read()
if not raw.strip():
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items") or []
for item in items:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    name = meta.get("name")
    phase = status.get("phase")
    nfs = spec.get("nfs") or {}
    claim = (spec.get("claimRef") or {}).get("name")
    print(f"pv={name} phase={phase} claim={claim} nfs={nfs}")
PY
  rm -f "${tmp_pvs}"

  log "StorageClass list"
  tmp_sc=$(mktemp)
  req GET "${APISHIM_URL}/apis/storage.k8s.io/v1/storageclasses" >"${tmp_sc}"
  "${PYTHON_BIN}" - "${tmp_sc}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read()
if not raw.strip():
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items") or []
for item in items:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    print(f"sc={meta.get('name')} provisioner={spec.get('provisioner')} params={spec.get('parameters')}")
PY
  rm -f "${tmp_sc}"
  set -e
}

cleanup() {
  log "cleaning up resources"
  delete_if_exists "${APISHIM_URL}/apis/apps/v1/namespaces/${NS}/deployments/${CLONE_DEPLOY}"
  delete_if_exists "${APISHIM_URL}/apis/apps/v1/namespaces/${NS}/deployments/${SRC_DEPLOY}"
  delete_if_exists "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${CLONE_PVC}"
  delete_if_exists "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${SRC_PVC}"
  delete_if_exists "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/namespaces/${NS}/volumesnapshots/${SNAP_NAME}"
  if [[ -n "${SNAP_CONTENT}" ]]; then
    delete_if_exists "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/volumesnapshotcontents/${SNAP_CONTENT}"
  fi
  delete_if_exists "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/volumesnapshotclasses/${SNAP_CLASS}"
  if [[ -n "${CLONE_PV}" ]]; then
    delete_if_exists "${APISHIM_URL}/api/v1/persistentvolumes/${CLONE_PV}"
  fi
  if [[ -n "${SRC_PV}" ]]; then
    delete_if_exists "${APISHIM_URL}/api/v1/persistentvolumes/${SRC_PV}"
  fi
}
trap cleanup EXIT

wait_pvc_bound() {
  local pvc_name=$1
  for _i in $(seq 1 45); do
    local resp
    resp=$(req GET "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${pvc_name}" || true)
    local phase pv uid
    local tmp_pvc parsed
    tmp_pvc=$(mktemp)
    printf '%s' "${resp}" >"${tmp_pvc}"
    parsed=$("${PYTHON_BIN}" - "${tmp_pvc}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read().strip()
if not raw:
    print("\t\t")
    raise SystemExit(0)
try:
    data = json.loads(raw)
except Exception:
    print("\t\t")
    raise SystemExit(0)
status = data.get("status") or {}
spec = data.get("spec") or {}
meta = data.get("metadata") or {}
phase = status.get("phase", "")
pv = spec.get("volumeName", "")
uid = meta.get("uid", "")
print(f"{phase}\t{pv}\t{uid}")
PY
)
    rm -f "${tmp_pvc}"
    read -r phase pv uid <<< "${parsed}"
    if [[ "${phase}" == "Bound" ]]; then
      printf '%s\t%s\n' "${pv}" "${uid}"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_mount() {
  local path=$1
  for _i in $(seq 1 60); do
    if grep -qs " ${path} " /proc/mounts; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_snapshot_ready() {
  for _i in $(seq 1 60); do
    local resp
    resp=$(req GET "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/namespaces/${NS}/volumesnapshots/${SNAP_NAME}" || true)
    local ready content
    local tmp_snap parsed
    tmp_snap=$(mktemp)
    printf '%s' "${resp}" >"${tmp_snap}"
    parsed=$("${PYTHON_BIN}" - "${tmp_snap}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    raw = fh.read().strip()
if not raw:
    print("	")
    raise SystemExit(0)
try:
    data = json.loads(raw)
except Exception:
    print("	")
    raise SystemExit(0)
status = data.get("status") or {}
ready = bool(status.get("readyToUse"))
content = status.get("boundVolumeSnapshotContentName", "")
print(f"{ready}	{content}")
PY
)
    rm -f "${tmp_snap}"
    read -r ready content <<< "${parsed}"
    if [[ "${ready}" == "True" || "${ready}" == "true" ]]; then
      SNAP_CONTENT=${content}
      return 0
    fi
    sleep 1
  done
  return 1
}

log "applying VolumeSnapshotClass ${SNAP_CLASS}"
cat <<EOF_SNAPCLASS | put_json "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/volumesnapshotclasses/${SNAP_CLASS}"
{
  "apiVersion": "snapshot.storage.k8s.io/v1",
  "kind": "VolumeSnapshotClass",
  "metadata": {
    "name": "${SNAP_CLASS}",
    "annotations": {
      "snapshot.storage.kubernetes.io/is-default-class": "true"
    }
  },
  "driver": "k1s.io/nfs",
  "deletionPolicy": "Delete"
}
EOF_SNAPCLASS

log "applying source PVC ${SRC_PVC}"
cat <<EOF_SRC_PVC | put_json "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${SRC_PVC}"
{
  "apiVersion": "v1",
  "kind": "PersistentVolumeClaim",
  "metadata": {"name": "${SRC_PVC}", "namespace": "${NS}"},
  "spec": {
    "accessModes": ["ReadWriteMany"],
    "storageClassName": "${SC}",
    "resources": {"requests": {"storage": "1Gi"}}
  }
}
EOF_SRC_PVC

log "applying source Deployment ${SRC_DEPLOY}"
cat <<EOF_SRC_DEPLOY | put_json "${APISHIM_URL}/apis/apps/v1/namespaces/${NS}/deployments/${SRC_DEPLOY}"
{
  "apiVersion": "apps/v1",
  "kind": "Deployment",
  "metadata": {"name": "${SRC_DEPLOY}", "namespace": "${NS}"},
  "spec": {
    "replicas": 1,
    "selector": {"matchLabels": {"app": "${SRC_DEPLOY}"}},
    "template": {
      "metadata": {"labels": {"app": "${SRC_DEPLOY}"}},
      "spec": {
        "containers": [
          {
            "name": "app",
            "image": "busybox:1.36",
            "command": ["sh", "-c", "sleep 3600"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}]
          }
        ],
        "volumes": [
          {"name": "data", "persistentVolumeClaim": {"claimName": "${SRC_PVC}"}}
        ]
      }
    }
  }
}
EOF_SRC_DEPLOY

log "waiting for source PVC to bind"
if ! src_status=$(wait_pvc_bound "${SRC_PVC}"); then
  dump_recent_events
  dump_storage_state
  log "source PVC did not reach Bound phase"
  exit 1
fi
SRC_PV=${src_status%%$'\t'*}
log "source PVC bound to PV ${SRC_PV:-<unknown>}"

log "waiting for source mount at ${SRC_MOUNT}"
if ! wait_mount "${SRC_MOUNT}"; then
  dump_recent_events
  log "source mount not detected at ${SRC_MOUNT}"
  exit 1
fi

log "writing source data"
mkdir -p "${SRC_MOUNT}"
echo "hello snapshot" >"${SRC_MOUNT}/data.txt"
flush_data "${SRC_MOUNT}/data.txt"

log "applying VolumeSnapshot ${SNAP_NAME}"
cat <<EOF_SNAP | put_json "${APISHIM_URL}/apis/snapshot.storage.k8s.io/v1/namespaces/${NS}/volumesnapshots/${SNAP_NAME}"
{
  "apiVersion": "snapshot.storage.k8s.io/v1",
  "kind": "VolumeSnapshot",
  "metadata": {"name": "${SNAP_NAME}", "namespace": "${NS}"},
  "spec": {
    "source": {
      "persistentVolumeClaimName": "${SRC_PVC}"
    }
  }
}
EOF_SNAP

log "waiting for snapshot to become ready"
if ! wait_snapshot_ready; then
  dump_recent_events
  log "snapshot did not reach readyToUse"
  exit 1
fi
log "snapshot ready via content ${SNAP_CONTENT:-<unknown>}"

log "applying clone PVC ${CLONE_PVC}"
cat <<EOF_CLONE_PVC | put_json "${APISHIM_URL}/api/v1/namespaces/${NS}/persistentvolumeclaims/${CLONE_PVC}"
{
  "apiVersion": "v1",
  "kind": "PersistentVolumeClaim",
  "metadata": {"name": "${CLONE_PVC}", "namespace": "${NS}"},
  "spec": {
    "accessModes": ["ReadWriteMany"],
    "storageClassName": "${SC}",
    "resources": {"requests": {"storage": "1Gi"}},
    "dataSource": {
      "apiGroup": "snapshot.storage.k8s.io",
      "kind": "VolumeSnapshot",
      "name": "${SNAP_NAME}"
    }
  }
}
EOF_CLONE_PVC

log "applying clone Deployment ${CLONE_DEPLOY}"
cat <<EOF_CLONE_DEPLOY | put_json "${APISHIM_URL}/apis/apps/v1/namespaces/${NS}/deployments/${CLONE_DEPLOY}"
{
  "apiVersion": "apps/v1",
  "kind": "Deployment",
  "metadata": {"name": "${CLONE_DEPLOY}", "namespace": "${NS}"},
  "spec": {
    "replicas": 1,
    "selector": {"matchLabels": {"app": "${CLONE_DEPLOY}"}},
    "template": {
      "metadata": {"labels": {"app": "${CLONE_DEPLOY}"}},
      "spec": {
        "containers": [
          {
            "name": "app",
            "image": "busybox:1.36",
            "command": ["sh", "-c", "sleep 3600"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"}]
          }
        ],
        "volumes": [
          {"name": "data", "persistentVolumeClaim": {"claimName": "${CLONE_PVC}"}}
        ]
      }
    }
  }
}
EOF_CLONE_DEPLOY

log "waiting for clone PVC to bind"
if ! clone_status=$(wait_pvc_bound "${CLONE_PVC}"); then
  dump_recent_events
  log "clone PVC did not reach Bound phase"
  exit 1
fi
CLONE_PV=${clone_status%%$'\t'*}
log "clone PVC bound to PV ${CLONE_PV:-<unknown>}"

log "waiting for clone mount at ${CLONE_MOUNT}"
if ! wait_mount "${CLONE_MOUNT}"; then
  dump_recent_events
  log "clone mount not detected at ${CLONE_MOUNT}"
  exit 1
fi

log "verifying cloned data"
if [[ ! -f "${CLONE_MOUNT}/data.txt" ]]; then
  dump_recent_events
  log "cloned data missing at ${CLONE_MOUNT}/data.txt"
  exit 1
fi
content=$(cat "${CLONE_MOUNT}/data.txt")
if [[ "${content}" != "hello snapshot" ]]; then
  dump_recent_events
  log "unexpected clone content: ${content}"
  exit 1
fi

log "snapshot/clone smoke test passed"
