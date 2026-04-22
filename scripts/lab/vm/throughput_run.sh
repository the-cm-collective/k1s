#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
LABEL="throughput"
ENDPOINT="${INFERENCE_URL:-}"
MINUTES=""
CONCURRENCY=""

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] --endpoint <url> [--label baseline|throughput] [--minutes N] [--concurrency N]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --minutes) MINUTES="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
[[ -n "$ENDPOINT" ]] || { err "--endpoint required (or set INFERENCE_URL)"; exit 2; }

variant_json="$(variant_to_json "$VARIANT")"
if [[ -z "$MINUTES" ]]; then
  MINUTES="$(echo "$variant_json" | jq -r '.baseline.duration_minutes')"
fi
if [[ -z "$CONCURRENCY" ]]; then
  CONCURRENCY="$(echo "$variant_json" | jq -r '.baseline.concurrency')"
fi

ensure_run_dir "$RUN_ID"
mkdir -p "$(run_dir "$RUN_ID")/loadgen"
out_file="$(run_dir "$RUN_ID")/loadgen/requests-${LABEL}.jsonl"
summary_file="$(run_dir "$RUN_ID")/loadgen/summary-${LABEL}.json"

python "$ROOT_DIR/scripts/lab/vm/loadgen.py" \
  --url "$ENDPOINT" \
  --run-id "$RUN_ID" \
  --minutes "$MINUTES" \
  --concurrency "$CONCURRENCY" \
  --out "$out_file" \
  --summary "$summary_file"

log "wrote $summary_file"
