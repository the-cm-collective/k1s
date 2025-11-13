#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[shim-demo] missing dependency: $1" >&2
    exit 2
  fi
}

need python
need helm
need kubectl
PYTHONPATH=${PYTHONPATH:-$ROOT_DIR/src}
PORT=${PORT:-8445}
TOKEN=${TOKEN:-helm-demo}
RUNTIME=${RUNTIME:-stub}
CHART_NAME=${CHART_NAME:-demochart}
NAMESPACE=${NAMESPACE:-demo}
TMPDIR=${TMPDIR:-/tmp}
WORKDIR="$(mktemp -d "$TMPDIR/helm-shim-XXXX")"
KUBECONFIG_PATH="$WORKDIR/kubeconfig"
LOG_PATH="$WORKDIR/shim.log"
CHART_DIR="$WORKDIR/$CHART_NAME"

cleanup() {
  local ec=$?
  if [[ -n "${SHIM_PID:-}" ]] && kill -0 "$SHIM_PID" 2>/dev/null; then
    kill "$SHIM_PID" 2>/dev/null || true
    wait "$SHIM_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
  exit $ec
}
trap cleanup EXIT

export PYTHONPATH
AE_APISHIM_RUNTIME="$RUNTIME" python -m ae.apishim serve \
  --host 127.0.0.1 --port "$PORT" --token "$TOKEN" >"$LOG_PATH" 2>&1 &
SHIM_PID=$!

python -m ae.apishim kubeconfig \
  --server "http://127.0.0.1:$PORT" \
  --token "$TOKEN" \
  --context k1s-shim \
  --insecure-skip-tls-verify > "$KUBECONFIG_PATH"
export KUBECONFIG="$KUBECONFIG_PATH"

mkdir "$CHART_DIR"
helm create "$CHART_DIR" >/dev/null
cat <<YAML > "$CHART_DIR/values.yaml"
replicaCount: 1
image:
  repository: nginx
  tag: "1.27"
service:
  type: NodePort
  # Leave nodePort unset to let the shim allocate within AE_APISHIM_NODEPORT range
resources: {}
ingress:
  enabled: true
  className: ""
  hosts:
    - host: demo.local
      paths:
        - path: /
          pathType: Prefix
  tls: []
YAML

helm dependency update "$CHART_DIR" >/dev/null

helm install "$CHART_NAME" "$CHART_DIR" -n "$NAMESPACE" --create-namespace --wait
kubectl -n "$NAMESPACE" get deploy,svc,ing

ASSIGNED_PORT=$(kubectl -n "$NAMESPACE" get svc "$CHART_NAME" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "n/a")
echo "[shim-demo] service nodePort: $ASSIGNED_PORT"

helm uninstall "$CHART_NAME" -n "$NAMESPACE"
kubectl delete namespace "$NAMESPACE" >/dev/null

echo "\nRun completed. Logs: $LOG_PATH"
