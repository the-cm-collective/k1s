#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AE_BIN="${AE_BIN:-}"
APISHIM_KUBECTL="${APISHIM_KUBECTL:-$ROOT_DIR/scripts/dev/apishim_kubectl.sh}"
LABCTL_BIN="${LABCTL_BIN:-$ROOT_DIR/scripts/lab/vm/labctl.sh}"
VALIDATOR_BIN="${VALIDATOR_BIN:-$ROOT_DIR/scripts/dev/netfs_validate.sh}"

GUEST_IP="${GUEST_IP:-}"
GUEST_USER="${GUEST_USER:-ae}"
GUEST_KEY="${GUEST_KEY:-$HOME/.ssh/id_rsa}"
GUEST_PORT="${GUEST_PORT:-22}"

NAMESPACE="${NAMESPACE:-default}"
STORAGE_CLASS="${STORAGE_CLASS:-k1s-nfs}"
PVC_NAME="${PVC_NAME:-core-a-netfs-pvc}"
WRITER_APP="${WRITER_APP:-netfs-core-a-writer}"
READER_APP="${READER_APP:-netfs-core-a-reader}"
MOUNT_PATH="${MOUNT_PATH:-/data}"
IMAGE="${IMAGE:-docker.io/library/busybox:1.36}"
WRITER_VALUE="${WRITER_VALUE:-host-a-netfs}"

NODE_ROLE="${NODE_ROLE:-hub}"
NODE_SITE="${NODE_SITE:-core-a}"

APISHIM_ENV="${APISHIM_ENV:-$ROOT_DIR/state/profiles/k1s-core/apishim.env}"
CONTROLLER_ENV="${CONTROLLER_ENV:-$ROOT_DIR/state/profiles/k1s-core/controller.env}"
APISHIM_SERVER="${APISHIM_SERVER:-https://127.0.0.1:8445}"

STATUS_TIMEOUT="${STATUS_TIMEOUT:-120}"
EXPORT_ROOT="${EXPORT_ROOT:-$ROOT_DIR/state/host-a-nfs-export/netfs}"
IPS_JSON=""
TMP_DIR=""

usage() {
  cat <<'USAGE'
Usage: scripts/dev/host_a_netfs_smoke.sh [options]

Runs the Host A NFS-backed PVC smoke against an already-running Host A lane:
controller/apishim healthy, guest node registered, and ae-host-a-nfs exporting
the configured host path.

This wrapper handles:
  - strict auth bootstrap for ae/kubectl
  - PVC apply through apishim kubectl
  - writer/reader AE workload apply
  - remote guest-CRI data-path validation
  - final PVC/export-root evidence capture

Options:
  --guest-ip <ip>           Guest node primary IP (default: resolve via labctl)
  --guest-user <user>       Guest SSH user (default: ae)
  --guest-key <path>        Guest SSH key (default: ~/.ssh/id_rsa)
  --guest-port <port>       Guest SSH port (default: 22)
  --namespace <ns>          Namespace (default: default)
  --storage-class <name>    StorageClass (default: k1s-nfs)
  --pvc-name <name>         PVC name (default: core-a-netfs-pvc)
  --writer-app <name>       Writer app name (default: netfs-core-a-writer)
  --reader-app <name>       Reader app name (default: netfs-core-a-reader)
  --mount-path <path>       Shared mount path (default: /data)
  --image <ref>             App image ref (default: docker.io/library/busybox:1.36)
  --writer-value <value>    Writer file content (default: host-a-netfs)
  --node-role <value>       Node selector role (default: hub)
  --node-site <value>       Node selector site (default: core-a)
  --apishim-env <path>      Strict-auth apishim env file
  --controller-env <path>   Strict-auth controller env file
  --server <url>            Strict-auth apishim server
  --status-timeout <secs>   ae status timeout (default: 120)
  --export-root <path>      Host export root for evidence (default: state/host-a-nfs-export/netfs)
  -h, --help                Show this help text
USAGE
}

log() {
  printf '[host-a-netfs-smoke] %s\n' "$*"
}

die() {
  printf '[host-a-netfs-smoke] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$IPS_JSON" ]]; then
    rm -f "$IPS_JSON"
  fi
  if [[ -n "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --guest-ip)
      GUEST_IP="${2:-}"
      shift 2
      ;;
    --guest-user)
      GUEST_USER="${2:-}"
      shift 2
      ;;
    --guest-key)
      GUEST_KEY="${2:-}"
      shift 2
      ;;
    --guest-port)
      GUEST_PORT="${2:-}"
      shift 2
      ;;
    --namespace)
      NAMESPACE="${2:-}"
      shift 2
      ;;
    --storage-class)
      STORAGE_CLASS="${2:-}"
      shift 2
      ;;
    --pvc-name)
      PVC_NAME="${2:-}"
      shift 2
      ;;
    --writer-app)
      WRITER_APP="${2:-}"
      shift 2
      ;;
    --reader-app)
      READER_APP="${2:-}"
      shift 2
      ;;
    --mount-path)
      MOUNT_PATH="${2:-}"
      shift 2
      ;;
    --image)
      IMAGE="${2:-}"
      shift 2
      ;;
    --writer-value)
      WRITER_VALUE="${2:-}"
      shift 2
      ;;
    --node-role)
      NODE_ROLE="${2:-}"
      shift 2
      ;;
    --node-site)
      NODE_SITE="${2:-}"
      shift 2
      ;;
    --apishim-env)
      APISHIM_ENV="${2:-}"
      shift 2
      ;;
    --controller-env)
      CONTROLLER_ENV="${2:-}"
      shift 2
      ;;
    --server)
      APISHIM_SERVER="${2:-}"
      shift 2
      ;;
    --status-timeout)
      STATUS_TIMEOUT="${2:-}"
      shift 2
      ;;
    --export-root)
      EXPORT_ROOT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -z "$AE_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/ae" ]]; then
    AE_BIN="$ROOT_DIR/.venv/bin/ae"
  elif command -v ae >/dev/null 2>&1; then
    AE_BIN="$(command -v ae)"
  else
    die "'ae' command not found"
  fi
fi

[[ -x "$APISHIM_KUBECTL" ]] || die "apishim kubectl helper not executable: $APISHIM_KUBECTL"
[[ -x "$LABCTL_BIN" ]] || die "labctl helper not executable: $LABCTL_BIN"
[[ -x "$VALIDATOR_BIN" ]] || die "validator not executable: $VALIDATOR_BIN"

AUTH_ARGS=(--apishim-env "$APISHIM_ENV" --controller-env "$CONTROLLER_ENV" --server "$APISHIM_SERVER")
eval "$("$AE_BIN" auth local --strict "${AUTH_ARGS[@]}")"

if [[ -z "$GUEST_IP" ]]; then
  IPS_JSON="$(mktemp "$ROOT_DIR/state/host-a-netfs-ips.XXXXXX.json")"
  "$LABCTL_BIN" host-a-gpu ips --json >"$IPS_JSON"
  GUEST_IP="$(
    python - "$IPS_JSON" <<'PY'
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload.get("primary_ip") or "")
PY
  )"
fi

[[ -n "$GUEST_IP" ]] || die "guest IP could not be resolved"

TMP_DIR="$(mktemp -d "$ROOT_DIR/state/host-a-netfs-smoke.XXXXXX")"

pvc_manifest="$TMP_DIR/pvc.yaml"
writer_manifest="$TMP_DIR/writer.yaml"
reader_manifest="$TMP_DIR/reader.yaml"

cat >"$pvc_manifest" <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ${STORAGE_CLASS}
  resources:
    requests:
      storage: 1Gi
EOF

cat >"$writer_manifest" <<EOF
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: ${WRITER_APP}
spec:
  image: ${IMAGE}
  replicas: 1
  command: ["sh", "-c", "echo ${WRITER_VALUE} > ${MOUNT_PATH}/hello.txt; sleep 3600"]
  pvcMounts:
    - claimName: ${PVC_NAME}
      mountPath: ${MOUNT_PATH}
  nodeSelector:
    role: ${NODE_ROLE}
    site: ${NODE_SITE}
EOF

cat >"$reader_manifest" <<EOF
apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: ${READER_APP}
spec:
  image: ${IMAGE}
  replicas: 1
  command: ["sh", "-c", "while true; do cat ${MOUNT_PATH}/hello.txt || true; sleep 5; done"]
  pvcMounts:
    - claimName: ${PVC_NAME}
      mountPath: ${MOUNT_PATH}
      readOnly: true
  nodeSelector:
    role: ${NODE_ROLE}
    site: ${NODE_SITE}
EOF

log "checking storage class ${STORAGE_CLASS}"
"$APISHIM_KUBECTL" "${AUTH_ARGS[@]}" --read-only get storageclass "$STORAGE_CLASS" -o yaml

log "applying PVC ${PVC_NAME} through apishim kubectl"
"$APISHIM_KUBECTL" "${AUTH_ARGS[@]}" apply -f "$pvc_manifest" --validate=false

log "applying writer/reader workloads"
"$AE_BIN" apply -f "$writer_manifest"
"$AE_BIN" apply -f "$reader_manifest"

log "waiting for writer/reader readiness"
"$AE_BIN" status "$WRITER_APP" --watch 2 --timeout "$STATUS_TIMEOUT" --wide --events
"$AE_BIN" status "$READER_APP" --watch 2 --timeout "$STATUS_TIMEOUT" --wide --events

log "validating shared data path against guest CRI host ${GUEST_IP}"
bash "$VALIDATOR_BIN" \
  --writer-app "$WRITER_APP" \
  --reader-app "$READER_APP" \
  --mount-path "$MOUNT_PATH" \
  --namespace "$NAMESPACE" \
  --runtime cri \
  --cri-host "$GUEST_IP" \
  --cri-user "$GUEST_USER" \
  --cri-key "$GUEST_KEY" \
  --cri-port "$GUEST_PORT"

log "capturing PVC and export-root evidence"
"$APISHIM_KUBECTL" "${AUTH_ARGS[@]}" --read-only get pvc "$PVC_NAME" -n "$NAMESPACE" -o wide
find "$EXPORT_ROOT" -maxdepth 2
