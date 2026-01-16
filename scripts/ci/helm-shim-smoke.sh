#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PORT="${HELM_SHIM_SMOKE_PORT:-${PORT:-8445}}"
TOKEN="${HELM_SHIM_SMOKE_TOKEN:-${TOKEN:-helm-demo}}"
RUNTIME="${HELM_SHIM_SMOKE_RUNTIME:-${RUNTIME:-stub}}"
NAMESPACE="${HELM_SHIM_SMOKE_NAMESPACE:-${NAMESPACE:-demo-helm}}"
CHART_NAME="${HELM_SHIM_SMOKE_CHART:-${CHART_NAME:-demochart}}"
HELM_TIMEOUT="${HELM_SHIM_SMOKE_TIMEOUT:-${HELM_TIMEOUT:-120s}}"
HELM_TEMPLATE_ONLY="${HELM_SHIM_SMOKE_TEMPLATE_ONLY:-${HELM_TEMPLATE_ONLY:-0}}"
PYTHONPATH="${PYTHONPATH:-$ROOT_DIR/src}"

log() { echo "[helm-shim-smoke] $*"; }

export PORT TOKEN RUNTIME NAMESPACE CHART_NAME HELM_TIMEOUT HELM_TEMPLATE_ONLY PYTHONPATH

log "Starting helm shim smoke run (namespace=${NAMESPACE}, chart=${CHART_NAME}, port=${PORT})"
bash "$ROOT_DIR/scripts/helm_shim_demo.sh"
