#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ITERATIONS="${ITERATIONS:-10}"
TOPOLOGY="${TOPOLOGY:-single-host}"
INCLUDE_FAULTS=0
FAULTS_CSV="${FAULTS_CSV:-specs-permission-drift,backend-unavailable}"
FAIL_FAST=0

RESULTS_DIR="${RESULTS_DIR:-$ROOT_DIR/state/test-results}"
RUN_STAMP="${RUN_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
SUMMARY_JSON="${SUMMARY_JSON:-$RESULTS_DIR/ingress-matrix-summary-${RUN_STAMP}.json}"
ITER_TSV=""

FAULT_CONTROLLER_START_CMD="${FAULT_CONTROLLER_START_CMD:-}"
FAULT_GATEWAY_START_CMD="${FAULT_GATEWAY_START_CMD:-}"
FAULT_NATS_RELOAD_CMD="${FAULT_NATS_RELOAD_CMD:-}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/test_ingress_matrix_repeat.sh [options] [-- <matrix args>]

Runs ingress capability matrix repeatedly and writes an aggregate summary.

Options:
  --iterations <n>                 Number of matrix runs (default: 10)
  --topology <single-host|multi-host>
                                   Topology passed to test_ingress_matrix_cri.sh
  --include-faults                 Run configured fault cycles before each iteration
  --faults <csv>                   Fault list for cycle mode
  --fail-fast                      Stop on first failed iteration
  --results-dir <path>             Output dir for per-run artifacts and summary JSON
  --summary-json <path>            Explicit summary JSON output path

  --fault-controller-start-cmd <cmd>
  --fault-gateway-start-cmd <cmd>
  --fault-nats-reload-cmd <cmd>
                                   Optional helpers passed to ingress_fault_injection.sh
  -h, --help                       Show help
USAGE
}

log() {
  printf '[ingress-matrix-repeat] %s\n' "$*"
}

die() {
  printf '[ingress-matrix-repeat] ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

trim() {
  local s="${1:-}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

split_csv() {
  local csv="$1"
  local -n out_ref="$2"
  local raw
  IFS=',' read -r -a raw <<<"$csv"
  out_ref=()
  local item trimmed
  for item in "${raw[@]}"; do
    trimmed="$(trim "$item")"
    [[ -n "$trimmed" ]] && out_ref+=("$trimmed")
  done
}

count_non_running_containers() {
  local json
  if command -v crictl >/dev/null 2>&1; then
    if sudo -n true >/dev/null 2>&1; then
      json="$(sudo -n crictl ps -a -o json 2>/dev/null || true)"
    else
      json="$(crictl ps -a -o json 2>/dev/null || true)"
    fi
    if [[ -n "$json" ]]; then
      JSON_PAYLOAD="$json" python - <<'PY'
import json
import os
try:
    payload = json.loads(os.environ.get("JSON_PAYLOAD", "") or "{}")
except Exception:
    print(0)
    raise SystemExit(0)
containers = payload.get("containers") or []
count = 0
for item in containers:
    state = str(item.get("state") or "")
    if state.lower() != "container_running":
        count += 1
print(count)
PY
      return
    fi
  fi
  printf '0\n'
}

extract_failed_rows() {
  local json_path="$1"
  [[ -f "$json_path" ]] || {
    printf '9999\n'
    return
  }
  python - "$json_path" <<'PY'
import json
import sys
path = sys.argv[1]
try:
    payload = json.loads(open(path, encoding="utf-8").read())
except Exception:
    print(9999)
    raise SystemExit(0)
summary = payload.get("summary") or {}
print(int(summary.get("failed_rows", 9999)))
PY
}

append_iteration_row() {
  local iter="$1"
  local status="$2"
  local duration="$3"
  local failed_rows="$4"
  local restart_delta="$5"
  local timeout_hits="$6"
  local result_json="$7"
  local log_file="$8"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$iter" "$status" "$duration" "$failed_rows" "$restart_delta" "$timeout_hits" "$result_json" "$log_file" \
    >> "$ITER_TSV"
}

write_summary_json() {
  local iter_tsv="$1"
  local out_json="$2"
  python - "$iter_tsv" "$out_json" <<'PY'
import json
import statistics
import sys
from pathlib import Path

tsv = Path(sys.argv[1])
out = Path(sys.argv[2])
rows = []
for line in tsv.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    it, status, duration, failed_rows, restart_delta, timeout_hits, result_json, log_file = line.split("\t")
    rows.append(
        {
            "iteration": int(it),
            "status": status,
            "duration_s": float(duration),
            "failed_rows": int(failed_rows),
            "restart_delta": int(restart_delta),
            "timeout_hits": int(timeout_hits),
            "result_json": result_json,
            "log_file": log_file,
        }
    )

durations = [row["duration_s"] for row in rows]
summary = {
    "iterations": len(rows),
    "passed_iterations": sum(1 for row in rows if row["status"] == "pass"),
    "failed_iterations": sum(1 for row in rows if row["status"] == "fail"),
    "total_failed_rows": sum(row["failed_rows"] for row in rows),
    "total_timeout_hits": sum(row["timeout_hits"] for row in rows),
    "total_restart_delta": sum(row["restart_delta"] for row in rows),
    "duration_s": {
        "min": min(durations) if durations else 0.0,
        "max": max(durations) if durations else 0.0,
        "mean": statistics.mean(durations) if durations else 0.0,
        "median": statistics.median(durations) if durations else 0.0,
    },
}

payload = {"summary": summary, "iterations": rows}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

declare -a MATRIX_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iterations)
      ITERATIONS="${2:-}"
      shift 2
      ;;
    --topology)
      TOPOLOGY="${2:-}"
      shift 2
      ;;
    --include-faults)
      INCLUDE_FAULTS=1
      shift
      ;;
    --faults)
      FAULTS_CSV="${2:-}"
      shift 2
      ;;
    --fail-fast)
      FAIL_FAST=1
      shift
      ;;
    --results-dir)
      RESULTS_DIR="${2:-}"
      shift 2
      ;;
    --summary-json)
      SUMMARY_JSON="${2:-}"
      shift 2
      ;;
    --fault-controller-start-cmd)
      FAULT_CONTROLLER_START_CMD="${2:-}"
      shift 2
      ;;
    --fault-gateway-start-cmd)
      FAULT_GATEWAY_START_CMD="${2:-}"
      shift 2
      ;;
    --fault-nats-reload-cmd)
      FAULT_NATS_RELOAD_CMD="${2:-}"
      shift 2
      ;;
    --)
      shift
      MATRIX_ARGS+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      MATRIX_ARGS+=("$1")
      shift
      ;;
  esac
done

[[ "$ITERATIONS" =~ ^[0-9]+$ ]] || die "--iterations must be an integer"
(( ITERATIONS > 0 )) || die "--iterations must be > 0"

case "$TOPOLOGY" in
  single-host|multi-host) ;;
  *) die "--topology must be single-host or multi-host" ;;
esac

need_cmd python
need_cmd rg
mkdir -p "$RESULTS_DIR"
ITER_TSV="$(mktemp)"

declare -a FAULTS=()
split_csv "$FAULTS_CSV" FAULTS

log "iterations=$ITERATIONS topology=$TOPOLOGY include_faults=$INCLUDE_FAULTS"
log "summary_json=$SUMMARY_JSON"

failed_iterations=0

for ((i = 1; i <= ITERATIONS; i++)); do
  iter_stamp="$(date -u +%Y%m%dT%H%M%SZ)-iter${i}"
  iter_json="$RESULTS_DIR/ingress-matrix-${iter_stamp}.json"
  iter_log="$RESULTS_DIR/ingress-matrix-${iter_stamp}.log"
  started="$(date +%s)"
  before_restart="$(count_non_running_containers)"

  if (( INCLUDE_FAULTS == 1 )); then
    for fault in "${FAULTS[@]}"; do
      [[ -n "$fault" ]] || continue
      fault_cmd=(
        "$ROOT_DIR/scripts/dev/ingress_fault_injection.sh"
        --fault "$fault"
        --action cycle
      )
      [[ -n "$FAULT_CONTROLLER_START_CMD" ]] && fault_cmd+=(--controller-start-cmd "$FAULT_CONTROLLER_START_CMD")
      [[ -n "$FAULT_GATEWAY_START_CMD" ]] && fault_cmd+=(--gateway-start-cmd "$FAULT_GATEWAY_START_CMD")
      [[ -n "$FAULT_NATS_RELOAD_CMD" ]] && fault_cmd+=(--nats-reload-cmd "$FAULT_NATS_RELOAD_CMD")
      log "iteration=$i fault-cycle=$fault"
      "${fault_cmd[@]}" | tee -a "$iter_log"
    done
  fi

  matrix_cmd=(
    "$ROOT_DIR/scripts/dev/test_ingress_matrix_cri.sh"
    --topology "$TOPOLOGY"
    --result-json "$iter_json"
  )
  matrix_cmd+=("${MATRIX_ARGS[@]}")

  log "iteration=$i start result_json=$iter_json"
  if "${matrix_cmd[@]}" | tee -a "$iter_log"; then
    iter_status="pass"
  else
    iter_status="fail"
  fi

  after_restart="$(count_non_running_containers)"
  ended="$(date +%s)"
  duration=$((ended - started))
  restart_delta=$((after_restart - before_restart))
  timeout_hits="$( (rg -n "timed out" "$iter_log" || true) | wc -l | tr -d ' ')"
  failed_rows="$(extract_failed_rows "$iter_json")"

  if [[ "$iter_status" == "pass" && "$failed_rows" -gt 0 ]]; then
    iter_status="fail"
  fi

  if [[ "$iter_status" == "fail" ]]; then
    failed_iterations=$((failed_iterations + 1))
  fi

  append_iteration_row "$i" "$iter_status" "$duration" "$failed_rows" "$restart_delta" "$timeout_hits" "$iter_json" "$iter_log"
  log "iteration=$i status=$iter_status duration=${duration}s failed_rows=$failed_rows restart_delta=$restart_delta timeout_hits=$timeout_hits"

  if (( FAIL_FAST == 1 )) && [[ "$iter_status" == "fail" ]]; then
    break
  fi
done

write_summary_json "$ITER_TSV" "$SUMMARY_JSON"
rm -f "$ITER_TSV"

log "summary_json=$SUMMARY_JSON failed_iterations=$failed_iterations"
if (( failed_iterations > 0 )); then
  exit 1
fi

log "PASS repeat matrix"
