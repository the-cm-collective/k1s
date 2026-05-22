#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<USAGE
Usage:
  $0 image build [args]
  $0 image verify [args]
  $0 image transfer [args]
  $0 host-a-gpu <render|create-overlay|create-seed|define|start|stop|undefine|preflight|ips> [args]
  $0 host prepare [args]
  $0 smoke [args]
  $0 variant up [args]
  $0 variant validate [args]
  $0 variant baseline [args]
  $0 variant throughput [args]
  $0 variant gate [args]
  $0 variant down [args]
  $0 collect [args]
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

subject="$1"
shift

case "$subject" in
  image)
    action="${1:-}"
    shift || true
    case "$action" in
      build) exec "$SCRIPT_DIR/image_build.sh" "$@" ;;
      verify) exec "$SCRIPT_DIR/image_verify.sh" "$@" ;;
      transfer) exec "$SCRIPT_DIR/image_transfer.sh" "$@" ;;
      *) usage; exit 2 ;;
    esac
    ;;
  host-a-gpu)
    exec "$SCRIPT_DIR/host_a_gpu_guest.py" "$@"
    ;;
  host)
    action="${1:-}"
    shift || true
    case "$action" in
      prepare) exec "$SCRIPT_DIR/host_prepare.sh" "$@" ;;
      *) usage; exit 2 ;;
    esac
    ;;
  smoke)
    exec "$SCRIPT_DIR/smoke.sh" "$@"
    ;;
  variant)
    action="${1:-}"
    shift || true
    case "$action" in
      up) exec "$SCRIPT_DIR/variant_up.sh" "$@" ;;
      validate) exec "$SCRIPT_DIR/variant_validate.sh" "$@" ;;
      baseline) exec "$SCRIPT_DIR/variant_baseline.sh" "$@" ;;
      throughput) exec "$SCRIPT_DIR/variant_throughput.sh" "$@" ;;
      gate) exec "$SCRIPT_DIR/variant_gate.sh" "$@" ;;
      down) exec "$SCRIPT_DIR/variant_down.sh" "$@" ;;
      *) usage; exit 2 ;;
    esac
    ;;
  collect)
    exec "$SCRIPT_DIR/collect.sh" "$@"
    ;;
  *)
    usage
    exit 2
    ;;
esac
