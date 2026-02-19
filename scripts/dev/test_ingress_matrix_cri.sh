#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

TOPOLOGY="${TOPOLOGY:-multi-host}"
CORE_HOST="${CORE_HOST:-127.0.0.1}"
EDGE_HOST="${EDGE_HOST:-127.0.0.1}"

CORE_INGRESS_URL="${CORE_INGRESS_URL:-}"
CORE_INGRESS_TLS_URL="${CORE_INGRESS_TLS_URL:-}"
CORE_PUBLIC_INGRESS_URL="${CORE_PUBLIC_INGRESS_URL:-}"
EDGE_LOCAL_LISTENER_URL="${EDGE_LOCAL_LISTENER_URL:-}"
EDGE_BACKEND_HOST="${EDGE_BACKEND_HOST:-}"
EDGE_BACKEND_SCHEME="${EDGE_BACKEND_SCHEME:-http}"

CORE_INGRESS_URL_SET=0
CORE_INGRESS_TLS_URL_SET=0
CORE_PUBLIC_INGRESS_URL_SET=0
EDGE_LOCAL_LISTENER_URL_SET=0
EDGE_BACKEND_HOST_SET=0

usage() {
  cat <<'USAGE'
Usage: scripts/dev/test_ingress_matrix_cri.sh [options]

Topology-aware wrapper for ingress capability matrix checks.
It delegates to scripts/dev/test_ingress_matrix_single_host.sh with endpoint
overrides for single-host or multi-host CRI lanes.

Options:
  --topology <single-host|multi-host>  Topology profile (default: multi-host)
  --core-host <host>                   Core host/IP (default: 127.0.0.1)
  --edge-host <host>                   Edge host/IP (default: 127.0.0.1)

  --core-ingress-url <url>             Override core ingress URL (HTTP)
  --core-ingress-tls-url <url>         Override core ingress URL (TLS)
  --core-public-ingress-url <url>      Override core public ingress URL
  --edge-local-listener-url <url>      Edge-local listener URL for data-plane checks
  --edge-backend-host <host>           Backend host for direct edge probes
  --edge-backend-scheme <http|https>   Backend scheme (default: http)

  -- ...                               Any remaining args are passed directly to
                                       test_ingress_matrix_single_host.sh
USAGE
}

log() {
  printf '[ingress-matrix-cri] %s\n' "$*"
}

die() {
  printf '[ingress-matrix-cri] ERROR: %s\n' "$*" >&2
  exit 1
}

declare -a PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --topology)
      TOPOLOGY="${2:-}"
      shift 2
      ;;
    --core-host)
      CORE_HOST="${2:-}"
      shift 2
      ;;
    --edge-host)
      EDGE_HOST="${2:-}"
      shift 2
      ;;
    --core-ingress-url)
      CORE_INGRESS_URL="${2:-}"
      CORE_INGRESS_URL_SET=1
      shift 2
      ;;
    --core-ingress-tls-url)
      CORE_INGRESS_TLS_URL="${2:-}"
      CORE_INGRESS_TLS_URL_SET=1
      shift 2
      ;;
    --core-public-ingress-url)
      CORE_PUBLIC_INGRESS_URL="${2:-}"
      CORE_PUBLIC_INGRESS_URL_SET=1
      shift 2
      ;;
    --edge-local-listener-url)
      EDGE_LOCAL_LISTENER_URL="${2:-}"
      EDGE_LOCAL_LISTENER_URL_SET=1
      shift 2
      ;;
    --edge-backend-host)
      EDGE_BACKEND_HOST="${2:-}"
      EDGE_BACKEND_HOST_SET=1
      shift 2
      ;;
    --edge-backend-scheme)
      EDGE_BACKEND_SCHEME="${2:-}"
      shift 2
      ;;
    --)
      shift
      PASSTHROUGH_ARGS+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

case "$TOPOLOGY" in
  single-host|multi-host) ;;
  *) die "--topology must be single-host or multi-host" ;;
esac

case "$EDGE_BACKEND_SCHEME" in
  http|https) ;;
  *) die "--edge-backend-scheme must be http or https" ;;
esac

if [[ "$TOPOLOGY" == "single-host" ]]; then
  CORE_HOST="${CORE_HOST:-127.0.0.1}"
  EDGE_HOST="${EDGE_HOST:-127.0.0.1}"
fi

if [[ "$CORE_INGRESS_URL_SET" -eq 0 ]]; then
  CORE_INGRESS_URL="http://${CORE_HOST}:10080/"
fi
if [[ "$CORE_INGRESS_TLS_URL_SET" -eq 0 ]]; then
  CORE_INGRESS_TLS_URL="https://${CORE_HOST}:10443/"
fi
if [[ "$CORE_PUBLIC_INGRESS_URL_SET" -eq 0 ]]; then
  CORE_PUBLIC_INGRESS_URL="$CORE_INGRESS_TLS_URL"
fi
if [[ "$EDGE_BACKEND_HOST_SET" -eq 0 ]]; then
  EDGE_BACKEND_HOST="$EDGE_HOST"
fi
if [[ "$EDGE_LOCAL_LISTENER_URL_SET" -eq 0 && "$TOPOLOGY" == "multi-host" ]]; then
  EDGE_LOCAL_LISTENER_URL="https://${EDGE_HOST}:11443/"
fi

log "topology=$TOPOLOGY core_host=$CORE_HOST edge_host=$EDGE_HOST"
log "core_ingress_url=$CORE_INGRESS_URL core_ingress_tls_url=$CORE_INGRESS_TLS_URL core_public_ingress_url=$CORE_PUBLIC_INGRESS_URL"
log "edge_local_listener_url=${EDGE_LOCAL_LISTENER_URL:-<unset>} edge_backend=${EDGE_BACKEND_SCHEME}://${EDGE_BACKEND_HOST}:<dynamic-port>"

declare -a CMD=(
  "$ROOT_DIR/scripts/dev/test_ingress_matrix_single_host.sh"
  --core-ingress-url "$CORE_INGRESS_URL"
  --core-ingress-tls-url "$CORE_INGRESS_TLS_URL"
  --core-public-ingress-url "$CORE_PUBLIC_INGRESS_URL"
  --edge-backend-host "$EDGE_BACKEND_HOST"
  --edge-backend-scheme "$EDGE_BACKEND_SCHEME"
)

if [[ -n "$EDGE_LOCAL_LISTENER_URL" ]]; then
  CMD+=(--edge-local-listener-url "$EDGE_LOCAL_LISTENER_URL")
fi

CMD+=("${PASSTHROUGH_ARGS[@]}")
exec "${CMD[@]}"
