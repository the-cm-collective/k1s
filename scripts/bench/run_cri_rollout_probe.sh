#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

log() { echo "[cri-rollout-probe] $*"; }
trap 'rc=$?; log "error at line $LINENO: $BASH_COMMAND (exit=$rc)"; exit $rc' ERR

usage() {
  cat <<'EOF'
Usage: scripts/bench/run_cri_rollout_probe.sh [options]

Runs a narrow CRI verify suite for rollout timing comparison:
  - steady-state replicas fixed at 5
  - rollout replicas fixed at 5
  - defaults to 3 runs and 30s snapshots

Options:
  --base LABEL                     Override BASE label prefix.
  --runs "1 2 3"                  Override RUNS (default: "1 2 3").
  --duration SECONDS               Override DURATION (default: 30).
  --during-capture-timing MODE     immediate|warm (default: immediate).
  --during-warm-capture-timing MODE
                                   immediate|warm (default: warm).
  --post-capture-timing MODE       warm|immediate (default: warm).
  --app PATH                       Manifest path (default: specs/examples/echo.yaml).
  --app-name NAME                  App name (default: echo).
  --help                           Show this help.
EOF
}

during_capture_timing="${BENCH_ROLLOUT_DURING_CAPTURE_TIMING:-immediate}"
during_warm_capture_timing="${BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING:-warm}"
post_capture_timing="${BENCH_ROLLOUT_POST_CAPTURE_TIMING:-warm}"
runs="${RUNS:-1 2 3}"
duration="${DURATION:-30}"
app="${APP:-specs/examples/echo.yaml}"
app_name="${APP_NAME:-echo}"
base="${BASE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      base="${2:?missing value for --base}"
      shift 2
      ;;
    --runs)
      runs="${2:?missing value for --runs}"
      shift 2
      ;;
    --duration)
      duration="${2:?missing value for --duration}"
      shift 2
      ;;
    --during-capture-timing)
      during_capture_timing="${2:?missing value for --during-capture-timing}"
      shift 2
      ;;
    --during-warm-capture-timing)
      during_warm_capture_timing="${2:?missing value for --during-warm-capture-timing}"
      shift 2
      ;;
    --post-capture-timing)
      post_capture_timing="${2:?missing value for --post-capture-timing}"
      shift 2
      ;;
    --app)
      app="${2:?missing value for --app}"
      shift 2
      ;;
    --app-name)
      app_name="${2:?missing value for --app-name}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$during_capture_timing" != "immediate" && "$during_capture_timing" != "warm" ]]; then
  echo "invalid --during-capture-timing: $during_capture_timing" >&2
  exit 2
fi
if [[ "$during_warm_capture_timing" != "immediate" && "$during_warm_capture_timing" != "warm" ]]; then
  echo "invalid --during-warm-capture-timing: $during_warm_capture_timing" >&2
  exit 2
fi
if [[ "$post_capture_timing" != "immediate" && "$post_capture_timing" != "warm" ]]; then
  echo "invalid --post-capture-timing: $post_capture_timing" >&2
  exit 2
fi

if [[ -z "$base" ]]; then
  base="r$(date +%Y%m%d-%H%M)-cri-runc-rollout-probe-${during_capture_timing}"
fi

log "base=$base runs=$runs duration=${duration}s during=$during_capture_timing during-warm=$during_warm_capture_timing post=$post_capture_timing"

BASE="$base" \
RUNS="$runs" \
PURGE_EXISTING_RUNS="${PURGE_EXISTING_RUNS:-1}" \
APP="$app" \
APP_NAME="$app_name" \
DURATION="$duration" \
REPLICAS="5" \
ROLL_REPLICAS="5" \
BENCH_ROLLOUT_DURING_CAPTURE_TIMING="$during_capture_timing" \
BENCH_ROLLOUT_DURING_WARM_CAPTURE_TIMING="$during_warm_capture_timing" \
BENCH_ROLLOUT_POST_CAPTURE_TIMING="$post_capture_timing" \
AE_USE_REGISTRY_CACHE="${AE_USE_REGISTRY_CACHE:-0}" \
./scripts/bench/run_cri_verify.sh
