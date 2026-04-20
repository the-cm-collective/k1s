#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

log() { echo "[cri-candidate] $*"; }
have() { command -v "$1" >/dev/null 2>&1; }
trap 'rc=$?; log "error at line $LINENO: $BASH_COMMAND (exit=$rc)"; exit $rc' ERR

ensure_label_base() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    raw="r$(date +%Y%m%d-%H%M)-cri+exp+candidate"
  fi
  if [[ "$raw" != *"+exp+"* ]]; then
    raw="${raw}+exp+candidate"
  fi
  printf '%s\n' "$raw"
}

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
CONTROL_STRATEGY="${CONTROL_STRATEGY:-parallel}"
ORDERED_STEADY_QUIET="${ORDERED_STEADY_QUIET:-1}"
ORDERED_STRATEGY="${ORDERED_STRATEGY:-ordered}"
GROUP_ID="${GROUP_ID:-cri-ordered-candidate-$(date +%Y%m%d-%H%M%S)}"
GROUP_ROOT="${GROUP_ROOT:-state/bench-experiments/${GROUP_ID}}"
LABEL_BASE_PREFIX="$(ensure_label_base "${LABEL_BASE_PREFIX:-}")"

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

if have sudo; then
  sudo -v
fi

run_experiment() {
  local scenario="$1"
  local run_id="$2"
  local steady_quiet="$3"
  local rollout_strategy="$4"
  local exp_dir="${GROUP_ROOT}/${scenario}-r${run_id}"
  local label_base="${LABEL_BASE_PREFIX}-${scenario}-r${run_id}"
  local experiment_id="${GROUP_ID}-${scenario}-r${run_id}"

  ./scripts/bench/bench_env_teardown.sh --env state/bench-cri/env.sh || true
  if have sudo; then
    sudo pkill -f "python .*ae\\.controller.*state/bench-cri/specs" || true
  fi

  log "scenario=${scenario} run=${run_id} output=${exp_dir}"
  if [[ -n "$rollout_strategy" ]]; then
    env \
      PATH="$PATH" \
      PYTHONPATH="$PYTHONPATH" \
      BENCH_EXPERIMENT_OUTPUT_ROOT="$exp_dir" \
      BENCH_EXPERIMENT_STEADY_QUIET="$steady_quiet" \
      BENCH_EXPERIMENT_ROLLOUT_STRATEGY="$rollout_strategy" \
      APP="$APP" \
      APP_NAME="$APP_NAME" \
      REPLICAS="$REPLICAS" \
      ROLL_REPLICAS="$ROLL_REPLICAS" \
      DURATION="$DURATION" \
      make bench-rollout-tuning-experiment \
        LANE=cri \
        LABEL_BASE="$label_base" \
        EXPERIMENT_ID="$experiment_id"
  else
    env \
      PATH="$PATH" \
      PYTHONPATH="$PYTHONPATH" \
      BENCH_EXPERIMENT_OUTPUT_ROOT="$exp_dir" \
      BENCH_EXPERIMENT_STEADY_QUIET="$steady_quiet" \
      APP="$APP" \
      APP_NAME="$APP_NAME" \
      REPLICAS="$REPLICAS" \
      ROLL_REPLICAS="$ROLL_REPLICAS" \
      DURATION="$DURATION" \
      make bench-rollout-tuning-experiment \
        LANE=cri \
        LABEL_BASE="$label_base" \
        EXPERIMENT_ID="$experiment_id"
  fi
}

log "group_id=${GROUP_ID} group_root=${GROUP_ROOT}"
log "label_base_prefix=${LABEL_BASE_PREFIX}"
log "runs=${RUNS_RAW}"
log "control_strategy=${CONTROL_STRATEGY} ordered_strategy=${ORDERED_STRATEGY}"

for run in "${runs[@]}"; do
  run_experiment baseline "$run" "$CONTROL_STEADY_QUIET" "$CONTROL_STRATEGY"
  run_experiment ordered "$run" "$ORDERED_STEADY_QUIET" "$ORDERED_STRATEGY"
done

if have sudo; then
  sudo make bench-fix-perms >/dev/null 2>&1 || true
fi

python scripts/bench/summarize_rollout_candidate.py "$GROUP_ROOT" \
  | tee "${GROUP_ROOT}/reports/candidate.txt"
python scripts/bench/summarize_rollout_candidate.py "$GROUP_ROOT" --json \
  > "${GROUP_ROOT}/reports/candidate.json"

cat > "${GROUP_ROOT}/reports/summary.txt" <<EOF
group_id=${GROUP_ID}
group_root=${GROUP_ROOT}
label_base_prefix=${LABEL_BASE_PREFIX}
runs=${RUNS_RAW}
control_steady_quiet=${CONTROL_STEADY_QUIET}
control_strategy=${CONTROL_STRATEGY}
ordered_steady_quiet=${ORDERED_STEADY_QUIET}
ordered_strategy=${ORDERED_STRATEGY}
candidate_report=${GROUP_ROOT}/reports/candidate.txt
candidate_json=${GROUP_ROOT}/reports/candidate.json
log_file=${log_file}
EOF

log "candidate summary: ${GROUP_ROOT}/reports/candidate.txt"
log "candidate json: ${GROUP_ROOT}/reports/candidate.json"
