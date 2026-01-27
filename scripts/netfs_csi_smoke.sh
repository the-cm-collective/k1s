#!/usr/bin/env bash
set -euo pipefail

APISHIM_URL=${APISHIM_URL:-http://127.0.0.1:8445}
TOKEN=${AE_APISHIM_TOKEN:-}
NS=${NETFS_NAMESPACE:-default}
SC=${NETFS_STORAGE_CLASS:-k1s-csi}
PV=${NETFS_PV_NAME:-netfs-csi-pv}
PVC=${NETFS_PVC_NAME:-netfs-csi-pvc}
DEPLOY=${NETFS_DEPLOYMENT_NAME:-netfs-csi-echo}
NODE_ID=${NETFS_NODE_ID:-netfs-node}
NETFS_ROOT=${NETFS_ROOT:-/var/lib/ae/netfs}

CSI_DRIVER=${CSI_DRIVER:-csi.example.com}
CSI_HANDLE=${CSI_HANDLE:-vol-demo}
CSI_STAGE_SECRET=${CSI_STAGE_SECRET:-csi-stage}
CSI_PUBLISH_SECRET=${CSI_PUBLISH_SECRET:-csi-publish}
CSI_MULTIATTACH=${NETFS_MULTIATTACH:-0}
CSI_CONFLICT_NODE=${NETFS_CONFLICT_NODE:-netfs-node-b}

CLEANUP=${NETFS_CLEANUP:-1}
BOUND_PV=""
CONFLICT_VA=""

log() {
  printf '[netfs-csi] %s\n' "$1"
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

cleanup() {
  if [[ "$CLEANUP" != "1" ]]; then
    return
  fi
  log "cleaning up resources"
  delete_if_exists "$APISHIM_URL/apis/apps/v1/namespaces/$NS/deployments/$DEPLOY"
  delete_if_exists "$APISHIM_URL/api/v1/namespaces/$NS/persistentvolumeclaims/$PVC"
  if [[ -n "$BOUND_PV" ]]; then
    delete_if_exists "$APISHIM_URL/api/v1/persistentvolumes/$BOUND_PV"
  fi
  delete_if_exists "$APISHIM_URL/apis/storage.k8s.io/v1/volumeattachments/${PVC}-${NODE_ID}"
  if [[ -n "$CONFLICT_VA" ]]; then
    delete_if_exists "$APISHIM_URL/apis/storage.k8s.io/v1/volumeattachments/${CONFLICT_VA}"
  fi
  delete_if_exists "$APISHIM_URL/apis/storage.k8s.io/v1/csidrivers/${CSI_DRIVER}"
  delete_if_exists "$APISHIM_URL/apis/storage.k8s.io/v1/csinodes/${NODE_ID}"
  delete_if_exists "$APISHIM_URL/api/v1/namespaces/$NS/secrets/${CSI_STAGE_SECRET}"
  delete_if_exists "$APISHIM_URL/api/v1/namespaces/$NS/secrets/${CSI_PUBLISH_SECRET}"
}
trap cleanup EXIT

wait_pvc_bound() {
  local pvc_name=$1
  for _i in $(seq 1 30); do
    local resp
    resp=$(req GET "$APISHIM_URL/api/v1/namespaces/$NS/persistentvolumeclaims/$pvc_name" || true)
    local phase pv
    read -r phase pv <<EOF_STATUS
$(python - "$resp" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
raw = raw.strip()
if not raw:
    print("\t")
    raise SystemExit(0)
try:
    data = json.loads(raw)
except Exception:
    print("\t")
    raise SystemExit(0)
status = data.get("status") or {}
spec = data.get("spec") or {}
phase = status.get("phase", "")
volume = spec.get("volumeName", "")
print(f"{phase}\t{volume}")
PY
)
EOF_STATUS
    if [[ "$phase" == "Bound" ]]; then
      printf '%s\n' "${pv}"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_volume_attachment() {
  local pv_name=$1
  for _i in $(seq 1 30); do
    local resp
    resp=$(req GET "$APISHIM_URL/apis/storage.k8s.io/v1/volumeattachments" || true)
    local found
    found=$(python - "$resp" "$pv_name" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
pv = sys.argv[2] if len(sys.argv) > 2 else ""
if not raw.strip():
    print("")
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items") or []
for item in items:
    spec = item.get("spec") or {}
    source = spec.get("source") or {}
    if pv and source.get("persistentVolumeName") == pv:
        print((item.get("metadata") or {}).get("name", ""))
        raise SystemExit(0)
print("")
PY
)
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_marker() {
  local path=$1
  for _i in $(seq 1 45); do
    if [[ -f "$path" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_event_reason() {
  local reason=$1
  for _i in $(seq 1 30); do
    local resp
    resp=$(req GET "$APISHIM_URL/api/v1/namespaces/$NS/events" || true)
    local found
    found=$(python - "$resp" "$reason" <<'PY'
import json
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
target = sys.argv[2] if len(sys.argv) > 2 else ""
if not raw.strip():
    print("")
    raise SystemExit(0)
data = json.loads(raw)
items = data.get("items") or []
for item in items:
    reason = item.get("reason")
    if reason is None:
        spec = item.get("spec") or {}
        reason = spec.get("reason")
    if str(reason or "") == target:
        print("yes")
        raise SystemExit(0)
print("")
PY
)
    if [[ -n "$found" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

log "applying CSIDriver ${CSI_DRIVER}"
cat <<EOF_DRIVER | put_json "$APISHIM_URL/apis/storage.k8s.io/v1/csidrivers/${CSI_DRIVER}"
{
  "apiVersion": "storage.k8s.io/v1",
  "kind": "CSIDriver",
  "metadata": {"name": "${CSI_DRIVER}"},
  "spec": {"attachRequired": true, "podInfoOnMount": false}
}
EOF_DRIVER

log "applying CSINode ${NODE_ID}"
cat <<EOF_NODE | put_json "$APISHIM_URL/apis/storage.k8s.io/v1/csinodes/${NODE_ID}"
{
  "apiVersion": "storage.k8s.io/v1",
  "kind": "CSINode",
  "metadata": {"name": "${NODE_ID}"},
  "spec": {"drivers": [{"name": "${CSI_DRIVER}", "nodeID": "${NODE_ID}"}]}
}
EOF_NODE

log "applying CSI secrets"
cat <<EOF_STAGE_SECRET | put_json "$APISHIM_URL/api/v1/namespaces/$NS/secrets/${CSI_STAGE_SECRET}"
{
  "apiVersion": "v1",
  "kind": "Secret",
  "metadata": {"name": "${CSI_STAGE_SECRET}", "namespace": "${NS}"},
  "type": "Opaque",
  "data": {"username": "stage-user", "password": "stage-pass"}
}
EOF_STAGE_SECRET

cat <<EOF_PUBLISH_SECRET | put_json "$APISHIM_URL/api/v1/namespaces/$NS/secrets/${CSI_PUBLISH_SECRET}"
{
  "apiVersion": "v1",
  "kind": "Secret",
  "metadata": {"name": "${CSI_PUBLISH_SECRET}", "namespace": "${NS}"},
  "type": "Opaque",
  "data": {"username": "publish-user", "password": "publish-pass"}
}
EOF_PUBLISH_SECRET

log "applying PV $PV"
BOUND_PV=$PV
cat <<EOF_PV | put_json "$APISHIM_URL/api/v1/persistentvolumes/$PV"
{
  "apiVersion": "v1",
  "kind": "PersistentVolume",
  "metadata": {"name": "${PV}"},
  "spec": {
    "capacity": {"storage": "1Gi"},
    "accessModes": ["ReadWriteOnce"],
    "persistentVolumeReclaimPolicy": "Retain",
    "storageClassName": "${SC}",
    "csi": {
      "driver": "${CSI_DRIVER}",
      "volumeHandle": "${CSI_HANDLE}",
      "fsType": "ext4",
      "nodeStageSecretRef": {"name": "${CSI_STAGE_SECRET}", "namespace": "${NS}"},
      "nodePublishSecretRef": {"name": "${CSI_PUBLISH_SECRET}", "namespace": "${NS}"}
    }
  }
}
EOF_PV

if [[ "${CSI_MULTIATTACH}" == "1" ]]; then
  CONFLICT_VA="va-${PV}-${CSI_CONFLICT_NODE}"
  log "injecting conflict VolumeAttachment ${CONFLICT_VA}"
  cat <<EOF_VA | put_json "$APISHIM_URL/apis/storage.k8s.io/v1/volumeattachments/${CONFLICT_VA}"
{
  "apiVersion": "storage.k8s.io/v1",
  "kind": "VolumeAttachment",
  "metadata": {"name": "${CONFLICT_VA}"},
  "spec": {
    "attacher": "${CSI_DRIVER}",
    "nodeName": "${CSI_CONFLICT_NODE}",
    "source": {"persistentVolumeName": "${PV}"}
  },
  "status": {"attached": true}
}
EOF_VA
fi

log "applying PVC $PVC"
cat <<EOF_PVC | put_json "$APISHIM_URL/api/v1/namespaces/$NS/persistentvolumeclaims/$PVC"
{
  "apiVersion": "v1",
  "kind": "PersistentVolumeClaim",
  "metadata": {
    "name": "${PVC}",
    "namespace": "${NS}",
    "annotations": {"volume.kubernetes.io/selected-node": "${NODE_ID}"}
  },
  "spec": {
    "accessModes": ["ReadWriteOnce"],
    "storageClassName": "${SC}",
    "resources": {"requests": {"storage": "1Gi"}}
  }
}
EOF_PVC

log "applying Deployment $DEPLOY"
cat <<EOF_DEP | put_json "$APISHIM_URL/apis/apps/v1/namespaces/$NS/deployments/$DEPLOY"
{
  "apiVersion": "apps/v1",
  "kind": "Deployment",
  "metadata": {"name": "${DEPLOY}", "namespace": "${NS}"},
  "spec": {
    "replicas": 1,
    "selector": {"matchLabels": {"app": "${DEPLOY}"}},
    "template": {
      "metadata": {"labels": {"app": "${DEPLOY}"}},
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
          {"name": "data", "persistentVolumeClaim": {"claimName": "${PVC}"}}
        ]
      }
    }
  }
}
EOF_DEP

log "waiting for PVC to bind"
if ! pv_name=$(wait_pvc_bound "$PVC"); then
  log "PVC did not reach Bound phase"
  exit 1
fi
if [[ -n "$pv_name" ]]; then
  BOUND_PV=$pv_name
fi
log "PVC bound to PV ${BOUND_PV:-<unknown>}"

if [[ "${CSI_MULTIATTACH}" == "1" ]]; then
  log "waiting for MultiAttachForbidden event"
  if ! wait_event_reason "MultiAttachForbidden"; then
    log "MultiAttachForbidden event not observed"
    exit 1
  fi
  log "multi-attach blocked as expected"
  exit 0
fi

log "waiting for VolumeAttachment"
if ! va_name=$(wait_volume_attachment "${BOUND_PV}"); then
  log "VolumeAttachment did not appear"
  exit 1
fi
log "VolumeAttachment ${va_name}"

marker="${NETFS_ROOT}/${NS}/${PVC}/.csi-volume"
log "waiting for CSI marker at ${marker}"
if ! wait_marker "${marker}"; then
  log "CSI marker not detected"
  exit 1
fi

if ! grep -q "driver=${CSI_DRIVER}" "${marker}"; then
  log "CSI marker missing driver"
  exit 1
fi
if ! grep -q "volumeHandle=${CSI_HANDLE}" "${marker}"; then
  log "CSI marker missing volumeHandle"
  exit 1
fi
if ! grep -q "nodeStageSecretRef=${NS}/${CSI_STAGE_SECRET}" "${marker}"; then
  log "CSI marker missing nodeStageSecretRef"
  exit 1
fi
if ! grep -q "nodePublishSecretRef=${NS}/${CSI_PUBLISH_SECRET}" "${marker}"; then
  log "CSI marker missing nodePublishSecretRef"
  exit 1
fi
if ! grep -q "nodeStageSecretRef.keys=password,username" "${marker}"; then
  log "CSI marker missing nodeStageSecretRef keys"
  exit 1
fi
if ! grep -q "nodePublishSecretRef.keys=password,username" "${marker}"; then
  log "CSI marker missing nodePublishSecretRef keys"
  exit 1
fi

log "CSI smoke test passed"
