#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VARIANT=""
RUN_ID="${RUN_ID:-}"
ENDPOINT="${INFERENCE_URL:-}"

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] --endpoint <url>
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { echo "--variant required" >&2; exit 2; }
[[ -n "$ENDPOINT" ]] || { echo "--endpoint required" >&2; exit 2; }

if [[ -z "$RUN_ID" ]]; then
  RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
fi

"$SCRIPT_DIR/throughput_run.sh" --variant "$VARIANT" --run-id "$RUN_ID" --endpoint "$ENDPOINT" --label throughput
"$SCRIPT_DIR/collect_baseline.sh" --variant "$VARIANT" --run-id "$RUN_ID"
