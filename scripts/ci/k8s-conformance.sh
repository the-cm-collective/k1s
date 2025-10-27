#!/usr/bin/env bash
set -euo pipefail

# Requires: kind, kubectl, python3

export PYTHONPATH=${PYTHONPATH:-}:src

CLUSTER_NAME="ae-conformance"
KIND_BIN="${KIND_BIN:-kind}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"

${KIND_BIN} create cluster --name "${CLUSTER_NAME}" --wait 60s
trap "${KIND_BIN} delete cluster --name ${CLUSTER_NAME}" EXIT

echo "Exporting hardened echo manifest..."
python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o /tmp/echo-k8s.yaml

echo "Server-side dry-run apply for echo..."
${KUBECTL_BIN} apply --dry-run=server -f /tmp/echo-k8s.yaml -n demo || exit 1

echo "Exporting multi-replica hardened manifest..."
python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o /tmp/echo-mr-k8s.yaml

echo "Server-side dry-run apply for echo-mr..."
${KUBECTL_BIN} apply --dry-run=server -f /tmp/echo-mr-k8s.yaml -n demo || exit 1

echo "Conformance OK"

echo "Installing kubeconform..."
curl -sSL -o /tmp/kubeconform.tar.gz https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz
tar -C /usr/local/bin -xzf /tmp/kubeconform.tar.gz kubeconform
chmod +x /usr/local/bin/kubeconform

echo "Validating exported YAMLs with kubeconform..."
kubeconform -strict -summary /tmp/echo-k8s.yaml /tmp/echo-mr-k8s.yaml
