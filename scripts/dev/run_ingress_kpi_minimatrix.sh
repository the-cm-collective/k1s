#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MATRIX_SCRIPT="${MATRIX_SCRIPT:-$ROOT_DIR/scripts/dev/test_ingress_matrix_single_host.sh}"
RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/state/test-results}"
RUN_STAMP_BASE="${RUN_STAMP_BASE:-$(date -u +%Y%m%dT%H%M%SZ)}"

MODES="${MODES:-core-proxy}"
ARCHETYPES="${ARCHETYPES:-ws-echo,lb-distribution,sticky-cookie}"
TIER="${TIER:-tier2}"
VALIDATION_PROFILE="${VALIDATION_PROFILE:-deep+perf}"
PERF_PROFILE="${PERF_PROFILE:-full}"
LB_PROOF_SCOPE="${LB_PROOF_SCOPE:-auto}"

CONCURRENCIES_CSV="${CONCURRENCIES_CSV:-30,50,70}"

# KPI guardrails (tune per host as needed)
PERF_MIN_RPS="${PERF_MIN_RPS:-220}"
PERF_MAX_P95_MS="${PERF_MAX_P95_MS:-300}"
PERF_MAX_P99_MS="${PERF_MAX_P99_MS:-500}"
PERF_MAX_ERROR_RATE="${PERF_MAX_ERROR_RATE:-0.01}"
WS_MIN_CONNECTED_RATIO="${WS_MIN_CONNECTED_RATIO:-1}"
WS_MAX_CONNECT_FAILURE_RATE="${WS_MAX_CONNECT_FAILURE_RATE:-0}"
WS_MAX_MESSAGE_LOSS="${WS_MAX_MESSAGE_LOSS:-0}"

log() {
  printf '[kpi-minimatrix] %s\n' "$*"
}

die() {
  printf '[kpi-minimatrix] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || die "jq is required"
[[ -x "$MATRIX_SCRIPT" ]] || die "missing executable matrix script: $MATRIX_SCRIPT"
mkdir -p "$RESULTS_DIR"

IFS=',' read -r -a CONCURRENCIES <<<"$CONCURRENCIES_CSV"
(( ${#CONCURRENCIES[@]} > 0 )) || die "no concurrencies selected"

declare -a RESULT_JSONS=()
declare -a RESULT_STATUSS=()
failed_lanes=0

for raw in "${CONCURRENCIES[@]}"; do
  conc="$(printf '%s' "$raw" | xargs)"
  [[ "$conc" =~ ^[0-9]+$ ]] || die "invalid concurrency: $conc"
  run_stamp="${RUN_STAMP_BASE}-c${conc}"
  result_json="$RESULTS_DIR/ingress-matrix-${run_stamp}.json"

  log "running concurrency=$conc result_json=$result_json"
  lane_status="pass"
  lane_rc=0
  if "$MATRIX_SCRIPT" \
    --modes "$MODES" \
    --archetypes "$ARCHETYPES" \
    --tier "$TIER" \
    --validation-profile "$VALIDATION_PROFILE" \
    --perf-profile "$PERF_PROFILE" \
    --perf-concurrency "$conc" \
    --lb-proof-scope "$LB_PROOF_SCOPE" \
    --perf-min-rps "$PERF_MIN_RPS" \
    --perf-max-p95-ms "$PERF_MAX_P95_MS" \
    --perf-max-p99-ms "$PERF_MAX_P99_MS" \
    --perf-max-error-rate "$PERF_MAX_ERROR_RATE" \
    --ws-min-connected-ratio "$WS_MIN_CONNECTED_RATIO" \
    --ws-max-connect-failure-rate "$WS_MAX_CONNECT_FAILURE_RATE" \
    --ws-max-message-loss "$WS_MAX_MESSAGE_LOSS" \
    --result-json "$result_json"; then
    :
  else
    lane_rc=$?
    lane_status="fail(${lane_rc})"
    failed_lanes=$((failed_lanes + 1))
    log "lane failed concurrency=$conc rc=$lane_rc (continuing)"
  fi

  RESULT_JSONS+=("$result_json")
  RESULT_STATUSS+=("$lane_status")
done

printf '\n'
printf '%-4s | %-9s | %-5s | %-5s | %-8s | %-8s | %-8s | %-8s | %-8s | %-8s | %s\n' \
  "conc" "lane" "rows" "fail" "avg_rps" "avg_p95" "avg_p99" "err_rate" "ws_conn" "ws_failr" "result_json"
printf '%s\n' '-----+-----------+-------+-------+----------+----------+----------+----------+----------+----------+-----------------------------'

for i in "${!RESULT_JSONS[@]}"; do
  result_json="${RESULT_JSONS[$i]}"
  lane_status="${RESULT_STATUSS[$i]}"
  if [[ ! -f "$result_json" ]]; then
    printf '%-4s | %-9s | %-5s | %-5s | %-8s | %-8s | %-8s | %-8s | %-8s | %-8s | %s\n' \
      "n/a" "$lane_status" "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "$result_json"
    continue
  fi
  jq -r '
    .rows as $rows |
    [
      ($rows[0].perf.http.concurrency // 0),
      (.summary.total_rows // 0),
      (.summary.failed_rows // 0),
      ((if ($rows|length) > 0 then (($rows | map(.perf.http.rps // 0) | add) / ($rows|length)) else 0 end) | tostring),
      ((if ($rows|length) > 0 then (($rows | map(.perf.http.latency.p95_ms // 0) | add) / ($rows|length)) else 0 end) | tostring),
      ((if ($rows|length) > 0 then (($rows | map(.perf.http.latency.p99_ms // 0) | add) / ($rows|length)) else 0 end) | tostring),
      ((if ($rows|length) > 0 then (($rows | map(.perf.http.error_rate // 0) | add) / ($rows|length)) else 0 end) | tostring),
      (([ $rows[] | select(.archetype=="ws-echo") | .evidence.ws.connected_ratio // 0 ][0] // 0) | tostring),
      (([ $rows[] | select(.archetype=="ws-echo") | ((.evidence.ws.connect_failures // 0) / ((.evidence.ws.attempted_connections // 0) | if . > 0 then . else 1 end)) ][0] // 0) | tostring),
      $ARGS.named.result_json,
      $ARGS.named.lane_status
    ] | @tsv
  ' "$result_json" --args "$lane_status" |
  awk -F'\t' '{ printf "%-4s | %-9s | %-5s | %-5s | %-8.2f | %-8.2f | %-8.2f | %-8.4f | %-8.3f | %-8.3f | %s\n", $1,$11,$2,$3,$4,$5,$6,$7,$8,$9,$10 }'
done

if (( failed_lanes > 0 )); then
  log "completed with failures: ${failed_lanes}/${#RESULT_JSONS[@]} lane(s) failed"
  exit 1
fi

log "done"
