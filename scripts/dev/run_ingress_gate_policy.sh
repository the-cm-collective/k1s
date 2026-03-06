#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MINIMATRIX_SCRIPT="${MINIMATRIX_SCRIPT:-$ROOT_DIR/scripts/dev/run_ingress_kpi_minimatrix.sh}"
RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/state/test-results}"
RUN_STAMP_BASE="${RUN_STAMP_BASE:-$(date -u +%Y%m%dT%H%M%SZ)-gate}"

MODES="${MODES:-core-proxy}"
ARCHETYPES="${ARCHETYPES:-ws-echo,lb-distribution,sticky-cookie}"
TIER="${TIER:-tier2}"
VALIDATION_PROFILE="${VALIDATION_PROFILE:-deep+perf}"
PERF_PROFILE="${PERF_PROFILE:-full}"
LB_PROOF_SCOPE="${LB_PROOF_SCOPE:-auto}"

GATE_CONCURRENCIES_CSV="${GATE_CONCURRENCIES_CSV:-30,50}"
RECHECK_CONCURRENCY="${RECHECK_CONCURRENCY:-50}"
RECHECK_REPEATS="${RECHECK_REPEATS:-3}"
RECHECK_MIN_PASS="${RECHECK_MIN_PASS:-2}"

# Jitter hardening prep (enabled by default).
GATE_PREP_ETCD="${GATE_PREP_ETCD:-1}"
ETCD_MAINT_SCRIPT="${ETCD_MAINT_SCRIPT:-$ROOT_DIR/scripts/dev/etcd_maintenance.sh}"

# Gate thresholds.
PERF_MIN_RPS="${PERF_MIN_RPS:-210}"
PERF_MAX_P95_MS="${PERF_MAX_P95_MS:-300}"
PERF_MAX_P99_MS="${PERF_MAX_P99_MS:-500}"
PERF_MAX_ERROR_RATE="${PERF_MAX_ERROR_RATE:-0.01}"
WS_MIN_CONNECTED_RATIO="${WS_MIN_CONNECTED_RATIO:-1}"
WS_MAX_CONNECT_FAILURE_RATE="${WS_MAX_CONNECT_FAILURE_RATE:-0}"
WS_MAX_MESSAGE_LOSS="${WS_MAX_MESSAGE_LOSS:-0}"

# Soft-fail windows.
SOFT_NEAR_MIN_RPS="${SOFT_NEAR_MIN_RPS:-205}"
SOFT_NEAR_MAX_P95_MS="${SOFT_NEAR_MAX_P95_MS:-315}"
SOFT_NEAR_MAX_P99_MS="${SOFT_NEAR_MAX_P99_MS:-525}"
SOFT_NEAR_MAX_ERROR_RATE="${SOFT_NEAR_MAX_ERROR_RATE:-0.01}"

# Hard-fail cutoffs.
HARD_MIN_RPS="${HARD_MIN_RPS:-200}"
HARD_MAX_P95_MS="${HARD_MAX_P95_MS:-330}"
HARD_MAX_P99_MS="${HARD_MAX_P99_MS:-550}"
HARD_MAX_ERROR_RATE="${HARD_MAX_ERROR_RATE:-0.01}"

log() {
  printf '[gate-policy] %s\n' "$*"
}

die() {
  printf '[gate-policy] ERROR: %s\n' "$*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || die "jq is required"
[[ -x "$MINIMATRIX_SCRIPT" ]] || die "missing executable minimatrix script: $MINIMATRIX_SCRIPT"
mkdir -p "$RESULTS_DIR"

decision_json="$RESULTS_DIR/gate-decision-${RUN_STAMP_BASE}.json"
analysis_tsv="$RESULTS_DIR/gate-analysis-${RUN_STAMP_BASE}.tsv"
: >"$analysis_tsv"

run_minimatrix() {
  local run_stamp="$1"
  local conc_csv="$2"
  RESULTS_DIR="$RESULTS_DIR" \
  RUN_STAMP_BASE="$run_stamp" \
  MODES="$MODES" \
  ARCHETYPES="$ARCHETYPES" \
  TIER="$TIER" \
  VALIDATION_PROFILE="$VALIDATION_PROFILE" \
  PERF_PROFILE="$PERF_PROFILE" \
  LB_PROOF_SCOPE="$LB_PROOF_SCOPE" \
  CONCURRENCIES_CSV="$conc_csv" \
  PERF_MIN_RPS="$PERF_MIN_RPS" \
  PERF_MAX_P95_MS="$PERF_MAX_P95_MS" \
  PERF_MAX_P99_MS="$PERF_MAX_P99_MS" \
  PERF_MAX_ERROR_RATE="$PERF_MAX_ERROR_RATE" \
  WS_MIN_CONNECTED_RATIO="$WS_MIN_CONNECTED_RATIO" \
  WS_MAX_CONNECT_FAILURE_RATE="$WS_MAX_CONNECT_FAILURE_RATE" \
  WS_MAX_MESSAGE_LOSS="$WS_MAX_MESSAGE_LOSS" \
  "$MINIMATRIX_SCRIPT"
}


gate_prepare_etcd() {
  local phase="$1"
  if [[ "$GATE_PREP_ETCD" != "1" ]]; then
    return
  fi
  if [[ ! -x "$ETCD_MAINT_SCRIPT" ]]; then
    log "WARN: etcd prep requested but script missing: $ETCD_MAINT_SCRIPT"
    return
  fi
  log "etcd prep ($phase): status"
  "$ETCD_MAINT_SCRIPT" status || true
  log "etcd prep ($phase): compact-defrag"
  "$ETCD_MAINT_SCRIPT" compact-defrag || true
}

collect_result_jsons() {
  local run_stamp="$1"
  local conc_csv="$2"
  local conc
  IFS=',' read -r -a __conc_list <<<"$conc_csv"
  for conc in "${__conc_list[@]}"; do
    conc="${conc//[[:space:]]/}"
    [[ -z "$conc" ]] && continue
    printf '%s/ingress-matrix-%s-c%s.json\n' "$RESULTS_DIR" "$run_stamp" "$conc"
  done
}

analyze_json() {
  local json="$1"
  [[ -f "$json" ]] || {
    log "missing result json: $json"
    return
  }
  jq -r \
    --arg json "$json" \
    --argjson perf_min_rps "$PERF_MIN_RPS" \
    --argjson perf_max_p95 "$PERF_MAX_P95_MS" \
    --argjson perf_max_p99 "$PERF_MAX_P99_MS" \
    --argjson perf_max_err "$PERF_MAX_ERROR_RATE" \
    --argjson soft_min_rps "$SOFT_NEAR_MIN_RPS" \
    --argjson soft_max_p95 "$SOFT_NEAR_MAX_P95_MS" \
    --argjson soft_max_p99 "$SOFT_NEAR_MAX_P99_MS" \
    --argjson soft_max_err "$SOFT_NEAR_MAX_ERROR_RATE" \
    --argjson hard_min_rps "$HARD_MIN_RPS" \
    --argjson hard_max_p95 "$HARD_MAX_P95_MS" \
    --argjson hard_max_p99 "$HARD_MAX_P99_MS" \
    --argjson hard_max_err "$HARD_MAX_ERROR_RATE" '
      .rows[]? as $r
      | select(($r.status // "pass") != "pass")
      | ($r.evidence // {}) as $ev
      | [ $ev | .. | objects | select(has("pass")) | .pass ] as $deep_vals
      | ($deep_vals | length) as $deep_count
      | ($deep_count > 0 and ($deep_vals | all(. == true))) as $deep_pass
      | ($r.perf.http.rps // 0) as $rps
      | ($r.perf.http.latency.p95_ms // 0) as $p95
      | ($r.perf.http.latency.p99_ms // 0) as $p99
      | ($r.perf.http.error_rate // 0) as $err
      | ($rps < $perf_min_rps) as $miss_rps
      | ($p95 > $perf_max_p95) as $miss_p95
      | ($p99 > $perf_max_p99) as $miss_p99
      | ($err > $perf_max_err) as $miss_err
      | ($miss_rps or $miss_p95 or $miss_p99 or $miss_err) as $has_perf_miss
      | (($rps < $hard_min_rps) or ($p95 > $hard_max_p95) or ($p99 > $hard_max_p99) or ($err > $hard_max_err)) as $severe_perf_miss
      | ((($miss_rps | not) or $rps >= $soft_min_rps)
         and (($miss_p95 | not) or $p95 <= $soft_max_p95)
         and (($miss_p99 | not) or $p99 <= $soft_max_p99)
         and (($miss_err | not) or $err <= $soft_max_err)) as $near_threshold
      | (if ((($deep_pass | not)) or $severe_perf_miss) then "hard"
         elif ($has_perf_miss and $near_threshold) then "soft"
         else "hard"
         end) as $classification
      | [
          $json,
          ($r.archetype // "unknown"),
          ($r.status // "fail"),
          ($deep_pass | tostring),
          ($rps | tostring),
          ($p95 | tostring),
          ($p99 | tostring),
          ($err | tostring),
          ($miss_rps | tostring),
          ($miss_p95 | tostring),
          ($miss_p99 | tostring),
          ($miss_err | tostring),
          $classification
        ] | @tsv
    ' "$json" >>"$analysis_tsv"
}

print_analysis() {
  if [[ ! -s "$analysis_tsv" ]]; then
    return
  fi
  printf '\n'
  printf '%-32s | %-14s | %-5s | %-4s | %-8s | %-8s | %-8s | %-8s | %-5s | %-5s | %-5s | %-5s | %s\n' \
    "json" "archetype" "status" "deep" "rps" "p95" "p99" "err" "rps?" "p95?" "p99?" "err?" "class"
  printf '%s\n' '---------------------------------+----------------+-------+------+----------+----------+----------+----------+-------+-------+-------+-------+-------'
  awk -F'\t' '{
    gsub(/^.*\//, "", $1);
    printf "%-32s | %-14s | %-5s | %-4s | %-8.2f | %-8.2f | %-8.2f | %-8.4f | %-5s | %-5s | %-5s | %-5s | %s\n",
      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
  }' "$analysis_tsv"
}

write_decision_json() {
  local verdict="$1"
  local reason="$2"
  local gate_rc="$3"
  local failed_rows="$4"
  local soft_rows="$5"
  local hard_rows="$6"
  local recheck_passes="$7"
  local recheck_total="$8"
  jq -n \
    --arg verdict "$verdict" \
    --arg reason "$reason" \
    --arg run_stamp_base "$RUN_STAMP_BASE" \
    --arg gate_concurrencies "$GATE_CONCURRENCIES_CSV" \
    --arg gate_rc "$gate_rc" \
    --arg failed_rows "$failed_rows" \
    --arg soft_rows "$soft_rows" \
    --arg hard_rows "$hard_rows" \
    --arg recheck_passes "$recheck_passes" \
    --arg recheck_total "$recheck_total" \
    --arg perf_min_rps "$PERF_MIN_RPS" \
    --arg perf_max_p95_ms "$PERF_MAX_P95_MS" \
    --arg perf_max_p99_ms "$PERF_MAX_P99_MS" \
    --arg perf_max_error_rate "$PERF_MAX_ERROR_RATE" \
    --arg soft_near_min_rps "$SOFT_NEAR_MIN_RPS" \
    --arg soft_near_max_p95_ms "$SOFT_NEAR_MAX_P95_MS" \
    --arg soft_near_max_p99_ms "$SOFT_NEAR_MAX_P99_MS" \
    --arg hard_min_rps "$HARD_MIN_RPS" \
    --arg hard_max_p95_ms "$HARD_MAX_P95_MS" \
    --arg hard_max_p99_ms "$HARD_MAX_P99_MS" \
    --arg analysis_tsv "$analysis_tsv" \
    '{
      verdict: $verdict,
      reason: $reason,
      run_stamp_base: $run_stamp_base,
      gate: {
        concurrencies_csv: $gate_concurrencies,
        exit_code: ($gate_rc | tonumber),
        failed_rows: ($failed_rows | tonumber),
        soft_rows: ($soft_rows | tonumber),
        hard_rows: ($hard_rows | tonumber)
      },
      recheck: {
        passes: ($recheck_passes | tonumber),
        total: ($recheck_total | tonumber)
      },
      thresholds: {
        perf_min_rps: ($perf_min_rps | tonumber),
        perf_max_p95_ms: ($perf_max_p95_ms | tonumber),
        perf_max_p99_ms: ($perf_max_p99_ms | tonumber),
        perf_max_error_rate: ($perf_max_error_rate | tonumber),
        soft_near_min_rps: ($soft_near_min_rps | tonumber),
        soft_near_max_p95_ms: ($soft_near_max_p95_ms | tonumber),
        soft_near_max_p99_ms: ($soft_near_max_p99_ms | tonumber),
        hard_min_rps: ($hard_min_rps | tonumber),
        hard_max_p95_ms: ($hard_max_p95_ms | tonumber),
        hard_max_p99_ms: ($hard_max_p99_ms | tonumber)
      },
      artifacts: {
        analysis_tsv: $analysis_tsv
      }
    }' >"$decision_json"
}

log "running Tier A gate (blocking) concurrencies=$GATE_CONCURRENCIES_CSV"
gate_prepare_etcd "tier-a"
gate_run_stamp="${RUN_STAMP_BASE}-gate"
gate_rc=0
if run_minimatrix "$gate_run_stamp" "$GATE_CONCURRENCIES_CSV"; then
  gate_rc=0
else
  gate_rc=$?
fi

while IFS= read -r json; do
  analyze_json "$json"
done < <(collect_result_jsons "$gate_run_stamp" "$GATE_CONCURRENCIES_CSV")

if [[ "$gate_rc" -eq 0 ]]; then
  write_decision_json "PASS" "all Tier A rows passed" "$gate_rc" 0 0 0 0 0
  log "verdict=PASS reason='all Tier A rows passed'"
  log "decision_json=$decision_json"
  exit 0
fi

failed_rows=0
soft_rows=0
hard_rows=0
if [[ -s "$analysis_tsv" ]]; then
  failed_rows="$(wc -l <"$analysis_tsv" | xargs)"
  soft_rows="$(awk -F'\t' '$13=="soft"{n++} END{print n+0}' "$analysis_tsv")"
  hard_rows="$(awk -F'\t' '$13=="hard"{n++} END{print n+0}' "$analysis_tsv")"
fi

print_analysis

if (( failed_rows == 1 && soft_rows == 1 && hard_rows == 0 )); then
  log "gate classified as SOFT_FAIL; starting immediate c${RECHECK_CONCURRENCY} recheck x${RECHECK_REPEATS}"
  recheck_passes=0
  for i in $(seq 1 "$RECHECK_REPEATS"); do
    gate_prepare_etcd "recheck-$i"
    recheck_stamp="${RUN_STAMP_BASE}-recheck-c${RECHECK_CONCURRENCY}-r${i}"
    if run_minimatrix "$recheck_stamp" "$RECHECK_CONCURRENCY"; then
      recheck_passes=$((recheck_passes + 1))
      log "recheck ${i}/${RECHECK_REPEATS}: pass"
    else
      log "recheck ${i}/${RECHECK_REPEATS}: fail"
    fi
  done
  if (( recheck_passes >= RECHECK_MIN_PASS )); then
    write_decision_json \
      "PASS_WITH_RECHECK" \
      "soft perf miss recovered by immediate c${RECHECK_CONCURRENCY} recheck" \
      "$gate_rc" "$failed_rows" "$soft_rows" "$hard_rows" "$recheck_passes" "$RECHECK_REPEATS"
    log "verdict=PASS_WITH_RECHECK recheck=${recheck_passes}/${RECHECK_REPEATS}"
    log "decision_json=$decision_json"
    exit 0
  fi
  write_decision_json \
    "FAIL" \
    "soft perf miss did not recover in immediate c${RECHECK_CONCURRENCY} recheck" \
    "$gate_rc" "$failed_rows" "$soft_rows" "$hard_rows" "$recheck_passes" "$RECHECK_REPEATS"
  log "verdict=FAIL recheck=${recheck_passes}/${RECHECK_REPEATS}"
  log "decision_json=$decision_json"
  exit 1
fi

write_decision_json \
  "FAIL" \
  "hard fail or multiple failed rows in Tier A gate" \
  "$gate_rc" "$failed_rows" "$soft_rows" "$hard_rows" 0 0
log "verdict=FAIL reason='hard fail or multiple failed rows'"
log "decision_json=$decision_json"
exit 1
