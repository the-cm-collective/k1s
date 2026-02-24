#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
BASELINE_RUN_ID=""
CURRENT_RUN_ID=""

usage() {
  cat <<USAGE
Usage: $0 --variant <path> --baseline-run-id <id> --current-run-id <id>
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --baseline-run-id) BASELINE_RUN_ID="$2"; shift 2 ;;
    --current-run-id) CURRENT_RUN_ID="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
[[ -n "$BASELINE_RUN_ID" ]] || { err "--baseline-run-id required"; exit 2; }
[[ -n "$CURRENT_RUN_ID" ]] || { err "--current-run-id required"; exit 2; }

variant_json="$(variant_to_json "$VARIANT")"
min_tps_ratio="$(echo "$variant_json" | jq -r '.throughput_gate.min_tps_ratio')"
max_p95_ratio="$(echo "$variant_json" | jq -r '.throughput_gate.max_p95_ratio')"
max_error_rate="$(echo "$variant_json" | jq -r '.throughput_gate.max_error_rate')"

baseline="$(run_dir "$BASELINE_RUN_ID")/loadgen/summary-baseline.json"
current="$(run_dir "$CURRENT_RUN_ID")/loadgen/summary-throughput.json"
out="$(run_dir "$CURRENT_RUN_ID")/metrics/gate_result.json"
mkdir -p "$(dirname "$out")"

python "$ROOT_DIR/scripts/lab/vm/throughput_gate.py" \
  --baseline "$baseline" \
  --current "$current" \
  --out "$out" \
  --min-tps-ratio "$min_tps_ratio" \
  --max-p95-ratio "$max_p95_ratio" \
  --max-error-rate "$max_error_rate"

log "gate result written to $out"
