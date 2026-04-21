#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

log() { echo "[lane-candidate] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }
trap 'rc=$?; log "error at line $LINENO: $BASH_COMMAND (exit=$rc)"; exit $rc' ERR

ensure_label_base() {
  local lane="$1"
  local raw="${2:-}"
  if [[ -z "$raw" ]]; then
    raw="r$(date +%Y%m%d-%H%M)-${lane}+exp+candidate"
  fi
  if [[ "$raw" != *"+exp+"* ]]; then
    raw="${raw}+exp+candidate"
  fi
  printf '%s\n' "$raw"
}

LANE="${LANE:-}"
case "$LANE" in
  rootless|rootful|k1nd) ;;
  *)
    log "set LANE=rootless|rootful|k1nd"
    exit 2
    ;;
esac

RUNS_RAW="${RUNS:-1 2 3}"
RUNS_RAW="${RUNS_RAW//,/ }"
read -r -a runs <<<"$RUNS_RAW"
if (( ${#runs[@]} == 0 )); then
  log "no runs requested"
  exit 2
fi
for run in "${runs[@]}"; do
  if [[ ! "$run" =~ ^[0-9]+$ ]]; then
    log "invalid run id '${run}'"
    exit 2
  fi
done

APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-2,5}"
DURATION="${DURATION:-30}"
CONTROL_STEADY_QUIET="${CONTROL_STEADY_QUIET:-0}"
CONTROL_READY_STABLE_POLLS="${CONTROL_READY_STABLE_POLLS:-2}"
CONTROL_SETTLE_DELAY="${CONTROL_SETTLE_DELAY:-2}"
QUIET_STEADY_QUIET="${QUIET_STEADY_QUIET:-1}"
QUIET_READY_STABLE_POLLS="${QUIET_READY_STABLE_POLLS:-3}"
QUIET_SETTLE_DELAY="${QUIET_SETTLE_DELAY:-5}"
GROUP_ID="${GROUP_ID:-${LANE}-quiet-candidate-$(date +%Y%m%d-%H%M%S)}"
GROUP_ROOT="${GROUP_ROOT:-state/bench-experiments/${GROUP_ID}}"
LABEL_BASE_PREFIX="$(ensure_label_base "$LANE" "${LABEL_BASE_PREFIX:-}")"

if [[ -e "$GROUP_ROOT" ]]; then
  log "group root already exists: $GROUP_ROOT"
  exit 2
fi

mkdir -p "$GROUP_ROOT/reports"
log_file="${GROUP_ROOT}/reports/run.log"
exec > >(tee -a "$log_file") 2>&1
trap 'rc=$?; log "exit=$rc log=$log_file"' EXIT

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="${PYTHONPATH:-src}"

if [[ "$LANE" == "rootful" ]]; then
  if ! have sudo; then
    log "sudo is required for lane=rootful"
    exit 2
  fi
  sudo -v
elif have sudo; then
  sudo -n true >/dev/null 2>&1 || true
fi

run_experiment() {
  local scenario="$1"
  local run_id="$2"
  local steady_quiet="$3"
  local ready_stable_polls="$4"
  local settle_delay="$5"
  local exp_dir="${GROUP_ROOT}/${scenario}-r${run_id}"
  local label_base="${LABEL_BASE_PREFIX}-${scenario}-r${run_id}"
  local experiment_id="${GROUP_ID}-${scenario}-r${run_id}"

  log "lane=${LANE} scenario=${scenario} run=${run_id} output=${exp_dir}"
  env \
    PATH="$PATH" \
    PYTHONPATH="$PYTHONPATH" \
    BENCH_EXPERIMENT_OUTPUT_ROOT="$exp_dir" \
    BENCH_EXPERIMENT_STEADY_QUIET="$steady_quiet" \
    BENCH_READY_STABLE_POLLS="$ready_stable_polls" \
    BENCH_SETTLE_DELAY="$settle_delay" \
    APP="$APP" \
    APP_NAME="$APP_NAME" \
    REPLICAS="$REPLICAS" \
    ROLL_REPLICAS="$ROLL_REPLICAS" \
    DURATION="$DURATION" \
    make bench-rollout-tuning-experiment \
      LANE="$LANE" \
      LABEL_BASE="$label_base" \
      EXPERIMENT_ID="$experiment_id"
}

log "lane=${LANE} group_id=${GROUP_ID} group_root=${GROUP_ROOT}"
log "label_base_prefix=${LABEL_BASE_PREFIX}"
log "runs=${RUNS_RAW}"

for run in "${runs[@]}"; do
  run_experiment baseline "$run" "$CONTROL_STEADY_QUIET" "$CONTROL_READY_STABLE_POLLS" "$CONTROL_SETTLE_DELAY"
  run_experiment quiet "$run" "$QUIET_STEADY_QUIET" "$QUIET_READY_STABLE_POLLS" "$QUIET_SETTLE_DELAY"
done

if have sudo; then
  sudo make bench-fix-perms >/dev/null 2>&1 || true
fi

python scripts/bench/summarize_rollout_candidate.py "$GROUP_ROOT" \
  | tee "${GROUP_ROOT}/reports/candidate.txt"
python scripts/bench/summarize_rollout_candidate.py "$GROUP_ROOT" --json \
  > "${GROUP_ROOT}/reports/candidate.json"

cat > "${GROUP_ROOT}/reports/summary.txt" <<EOF
lane=${LANE}
group_id=${GROUP_ID}
group_root=${GROUP_ROOT}
label_base_prefix=${LABEL_BASE_PREFIX}
runs=${RUNS_RAW}
control_steady_quiet=${CONTROL_STEADY_QUIET}
control_ready_stable_polls=${CONTROL_READY_STABLE_POLLS}
control_settle_delay=${CONTROL_SETTLE_DELAY}
quiet_steady_quiet=${QUIET_STEADY_QUIET}
quiet_ready_stable_polls=${QUIET_READY_STABLE_POLLS}
quiet_settle_delay=${QUIET_SETTLE_DELAY}
candidate_report=${GROUP_ROOT}/reports/candidate.txt
candidate_json=${GROUP_ROOT}/reports/candidate.json
log_file=${log_file}
EOF

log "candidate summary: ${GROUP_ROOT}/reports/candidate.txt"
log "candidate json: ${GROUP_ROOT}/reports/candidate.json"
