#!/usr/bin/env bash
set -euo pipefail

SERVER="${AE_APISHIM_SERVER:-http://127.0.0.1:8445}"
TOKEN="${AE_APISHIM_TOKEN:-}"
NAMESPACE="${AE_APISHIM_NAMESPACE:-default}"
APP="${AE_APISHIM_APP:-echo-exec}"
MANIFEST="${AE_APISHIM_MANIFEST:-specs/examples/echo-exec.yaml}"
LOCAL_PF_PORT="${AE_APISHIM_PF_LOCAL:-18080}"
REMOTE_PF_PORT="${AE_APISHIM_PF_REMOTE:-8080}"

if [[ -z "${TOKEN}" ]]; then
  echo "error: AE_APISHIM_TOKEN is required"
  exit 1
fi
if ! command -v kubectl >/dev/null 2>&1; then
  echo "error: kubectl is required"
  exit 1
fi

tmpcfg="$(mktemp)"
cleanup() {
  rm -f "${tmpcfg}"
  if [[ -n "${PF_PID:-}" ]]; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

python -m ae.apishim kubeconfig \
  --server "${SERVER}" \
  --token "${TOKEN}" \
  --insecure-skip-tls-verify > "${tmpcfg}"
export KUBECONFIG="${tmpcfg}"

echo "apishim: ${SERVER}"
kubectl_version="$(kubectl version --client --short 2>/dev/null || true)"
if [[ -z "${kubectl_version}" ]]; then
  echo "kubectl: unknown (client --short unsupported)"
else
  echo "kubectl: ${kubectl_version}"
fi

echo "checking apishim /version..."
kubectl get --raw /version >/dev/null

echo "checking for pods in ${NAMESPACE}..."
pod="$(kubectl get pods -n "${NAMESPACE}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${pod}" ]]; then
  echo "no pods found; applying ${MANIFEST}"
  python -m ae.cli apply -f "${MANIFEST}"
  sleep 2
  pod="$(kubectl get pods -n "${NAMESPACE}" -o jsonpath='{.items[0].metadata.name}')"
fi
if [[ -z "${pod}" ]]; then
  echo "error: no pod available for exec/port-forward"
  exit 1
fi
echo "pod: ${pod}"

echo "kubectl exec smoke..."
kubectl exec -n "${NAMESPACE}" "${pod}" -- sh -c 'echo exec-ok'

echo "kubectl port-forward smoke..."
kubectl port-forward -n "${NAMESPACE}" "pod/${pod}" "${LOCAL_PF_PORT}:${REMOTE_PF_PORT}" >/tmp/k1s-pf.log 2>&1 &
PF_PID=$!
sleep 1
curl -fsS "http://127.0.0.1:${LOCAL_PF_PORT}/" | head -c 200 || true
kill "${PF_PID}" >/dev/null 2>&1 || true
unset PF_PID

if [[ "${K9S_SMOKE:-0}" == "1" ]]; then
  K9S_POD="${pod}" \
  K9S_NAMESPACE="${NAMESPACE}" \
  K9S_KUBECONFIG="${tmpcfg}" \
  K9S_PORT_FORWARD_LOCAL="${LOCAL_PF_PORT}" \
  K9S_PORT_FORWARD_PORT="${REMOTE_PF_PORT}" \
  scripts/dev/apishim_k9s_smoke.sh
else
  echo "k9s manual check (optional):"
  echo "  KUBECONFIG=${tmpcfg} k9s"
  echo "  - open shell on pod ${pod}"
  echo "  - port-forward ${REMOTE_PF_PORT} and curl localhost:${LOCAL_PF_PORT}"
fi
