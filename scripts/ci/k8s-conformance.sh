#!/usr/bin/env bash
set -euo pipefail

# Requires: kind, kubectl, python3

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/scripts/ci/lib.sh"

export PYTHONPATH=${PYTHONPATH:-}:src

CLUSTER_NAME="ae-conformance"
KIND_BIN="${KIND_BIN:-kind}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-/tmp/ae-kind.kubeconfig}"

${KIND_BIN} create cluster --name "${CLUSTER_NAME}" --wait 60s
trap "${KIND_BIN} delete cluster --name ${CLUSTER_NAME}" EXIT
${KIND_BIN} get kubeconfig --name "${CLUSTER_NAME}" >"${KUBECONFIG_PATH}"
export KUBECONFIG="${KUBECONFIG_PATH}"

REACHABLE_HOST="${K1S_DOCKER_PUBLISHED_HOST:-$(ci_docker_published_host)}"
if [[ "${REACHABLE_HOST}" != "127.0.0.1" && "${REACHABLE_HOST}" != "localhost" ]]; then
  echo "Rewriting kind kubeconfig server host to ${REACHABLE_HOST} for remote Docker access..."
  ci_append_no_proxy "${REACHABLE_HOST}"
  python - "${KUBECONFIG}" "${REACHABLE_HOST}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml

path = Path(sys.argv[1])
host = sys.argv[2]
data = yaml.safe_load(path.read_text(encoding="utf-8"))
for cluster_entry in data.get("clusters", []):
    cluster = cluster_entry.get("cluster") or {}
    server = str(cluster.get("server") or "").strip()
    if not server:
        continue
    parsed = urlparse(server)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        continue
    port = f":{parsed.port}" if parsed.port else ""
    cluster["server"] = urlunparse(
        parsed._replace(netloc=f"{host}{port}")
    )
    cluster.pop("certificate-authority-data", None)
    cluster["insecure-skip-tls-verify"] = True
path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY
fi

echo "Ensuring demo namespace exists..."
${KUBECTL_BIN} create namespace demo --dry-run=client -o yaml | ${KUBECTL_BIN} apply --validate=false -f -

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
ci_prepare_user_bin
curl -sSL -o /tmp/kubeconform.tar.gz https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz
tar -C "$HOME/.local/bin" -xzf /tmp/kubeconform.tar.gz kubeconform
chmod +x "$HOME/.local/bin/kubeconform"

echo "Validating exported YAMLs with kubeconform..."
kubeconform -strict -summary /tmp/echo-k8s.yaml /tmp/echo-mr-k8s.yaml
