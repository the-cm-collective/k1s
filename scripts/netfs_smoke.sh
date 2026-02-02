#!/usr/bin/env bash
set -euo pipefail

APISHIM_URL=${APISHIM_URL:-http://127.0.0.1:8445}
TOKEN=${AE_APISHIM_TOKEN:-}
NS=${NETFS_NAMESPACE:-default}
SC=${NETFS_STORAGE_CLASS:-k1s-nfs}
PV=${NETFS_PV_NAME:-netfs-pv}
PVC=${NETFS_PVC_NAME:-netfs-pvc}
DEPLOY=${NETFS_DEPLOYMENT_NAME:-netfs-echo}
CLEANUP=${NETFS_CLEANUP:-1}
NETFS_DYNAMIC=${NETFS_DYNAMIC:-0}
NFS_SERVER=${NFS_SERVER:-127.0.0.1}
NFS_PATH=${NFS_PATH:-/exports/netfs}
BOUND_PV=""

log() {
  printf '[netfs-smoke] %s\n' "$1"
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
  req DELETE "$url" >/dev/null || true
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
}
trap cleanup EXIT

if [[ "$NETFS_DYNAMIC" != "1" ]]; then
  log "applying PV $PV"
  BOUND_PV=$PV
  cat <<EOF_PV | put_json "$APISHIM_URL/api/v1/persistentvolumes/$PV"
{
  "apiVersion": "v1",
  "kind": "PersistentVolume",
  "metadata": {"name": "${PV}"},
  "spec": {
    "capacity": {"storage": "1Gi"},
    "accessModes": ["ReadWriteMany"],
    "persistentVolumeReclaimPolicy": "Retain",
    "storageClassName": "${SC}",
    "nfs": {"server": "${NFS_SERVER}", "path": "${NFS_PATH}"}
  }
}
EOF_PV
else
  log "dynamic provisioning enabled; PV will be created by the storage controller"
fi

log "applying PVC $PVC"
cat <<EOF_PVC | put_json "$APISHIM_URL/api/v1/namespaces/$NS/persistentvolumeclaims/$PVC"
{
  "apiVersion": "v1",
  "kind": "PersistentVolumeClaim",
  "metadata": {"name": "${PVC}", "namespace": "${NS}"},
  "spec": {
    "accessModes": ["ReadWriteMany"],
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
for _i in $(seq 1 15); do
  resp=$(req GET "$APISHIM_URL/api/v1/namespaces/$NS/persistentvolumeclaims/$PVC" || true)
  phase_and_pv=$(python - "$resp" <<'PY'
import json,sys
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
  phase=${phase_and_pv%%$'\t'*}
  volume_name=${phase_and_pv#*$'\t'}
  if [[ "$phase" == "Bound" ]]; then
    if [[ -n "$volume_name" ]]; then
      BOUND_PV=$volume_name
    fi
    log "PVC bound to PV ${BOUND_PV:-<unknown>}"
    exit 0
  fi
  sleep 1
done

log "PVC did not reach Bound phase"
exit 1
