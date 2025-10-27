#!/usr/bin/env bash
set -euo pipefail

# Requires: k3d, kubectl, python3

export PYTHONPATH=${PYTHONPATH:-}:src

CLUSTER_NAME="ae-k3s"

k3d cluster create "$CLUSTER_NAME" --wait
trap "k3d cluster delete $CLUSTER_NAME" EXIT

echo "Exporting hardened echo manifest..."
python -m ae.cli export-k8s -f specs/examples/echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o /tmp/echo-k8s.yaml

echo "Server-side dry-run apply for echo..."
kubectl apply --dry-run=server -f /tmp/echo-k8s.yaml -n demo || exit 1

echo "Exporting multi-replica hardened manifest..."
python -m ae.cli export-k8s -f specs/examples/multi-replica-echo.yaml --namespace demo --preset web-hardened --ingress-class traefik --service-port 80 --validate -o /tmp/echo-mr-k8s.yaml

echo "Server-side dry-run apply for echo-mr..."
kubectl apply --dry-run=server -f /tmp/echo-mr-k8s.yaml -n demo || exit 1

echo "k3s conformance OK"

