#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="state/bench-env/env.sh"
VENV_DIR=".venv-demo"
LABEL="rollout-debug"
APP_MANIFEST=""
APP_NAME=""
REPLICAS=2
DURATION=30
POLL_INTERVAL=15
OUT_DIR="state/bench-env/debug"

usage() {
  cat <<'USAGE'
Usage: scripts/bench/run_rollout_debug.sh [options]

Options:
  --env <file>         Path to bench env file (default: state/bench-env/env.sh)
  --venv <dir>         Virtualenv directory to activate (default: .venv-demo)
  --app <path>         Manifest to target (default: BENCH_PRIMARY_MANIFEST from env)
  --app-name <name>    App name (default: BENCH_PRIMARY_APP from env)
  --replicas <n>       Replica count for rollout (default: 2)
  --duration <s>       Snapshot duration passed to rollout script (default: 30)
  --label <suffix>     Label suite suffix for rollout (default: rollout-debug-<date>)
  --interval <s>       Poll interval for status/events/history (default: 15)
  --out-dir <dir>      Directory to store collected logs (default: state/bench-env/debug)
  -h, --help           Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --app) APP_MANIFEST="$2"; shift 2 ;;
    --app-name) APP_NAME="$2"; shift 2 ;;
    --replicas) REPLICAS="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --interval) POLL_INTERVAL="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "env file not found: $ENV_FILE" >&2
  exit 2
fi
if [[ ! -d "$VENV_DIR" ]]; then
  echo "venv not found: $VENV_DIR" >&2
  exit 2
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
# shellcheck disable=SC1090
source "$ENV_FILE"

APP_MANIFEST=${APP_MANIFEST:-${BENCH_PRIMARY_MANIFEST:-}}
APP_NAME=${APP_NAME:-${BENCH_PRIMARY_APP:-}}
if [[ -z "$APP_MANIFEST" || -z "$APP_NAME" ]]; then
  echo "Unable to determine app manifest/name; pass --app and --app-name" >&2
  exit 2
fi

if [[ ! -f "$APP_MANIFEST" ]]; then
  echo "manifest not found: $APP_MANIFEST" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
DATE_TAG=$(date +%Y%m%d-%H%M%S)
LABEL_SUFFIX=${LABEL:-rollout-debug}
LABEL_SUITE="${LABEL_SUFFIX}-${DATE_TAG}"

STATUS_LOG="$OUT_DIR/${APP_NAME}-${DATE_TAG}-status.log"
EVENTS_LOG="$OUT_DIR/${APP_NAME}-${DATE_TAG}-events.log"
HISTORY_LOG="$OUT_DIR/${APP_NAME}-${DATE_TAG}-history.log"
PODMAN_LOG="$OUT_DIR/${APP_NAME}-${DATE_TAG}-podman.log"
ROLLOUT_LOG="$OUT_DIR/${APP_NAME}-${DATE_TAG}-rollout.log"

cat <<EOF2
[debug] env: $ENV_FILE
[debug] venv: $VENV_DIR
[debug] app: $APP_NAME ($APP_MANIFEST)
[debug] label: $LABEL_SUITE
[debug] replicas: $REPLICAS
[debug] duration: $DURATION
[debug] poll interval: $POLL_INTERVAL s
[debug] logs: $OUT_DIR
EOF2

poll_status() {
  while kill -0 "$1" 2>/dev/null; do
    {
      date -Iseconds | sed 's/^/[status] /'
      python -m ae.cli status "$APP_NAME" --wide --history 20 --events || true
      echo
    } >> "$STATUS_LOG"
    sleep "$POLL_INTERVAL" || break
  done
}

poll_events() {
  while kill -0 "$1" 2>/dev/null; do
    {
      date -Iseconds | sed 's/^/[events] /'
      python -m ae.cli events "$APP_NAME" --limit 50 || true
      echo
    } >> "$EVENTS_LOG"
    sleep "$POLL_INTERVAL" || break
  done
}

poll_history() {
  while kill -0 "$1" 2>/dev/null; do
    {
      date -Iseconds | sed 's/^/[history] /'
      python -m ae.cli history "$APP_NAME" --limit 50 || true
      echo
    } >> "$HISTORY_LOG"
    sleep "$POLL_INTERVAL" || break
  done
}

collect_podman_logs() {
  {
    date -Iseconds | sed 's/^/[podman] /'
    podman ps -a --filter "label=ae.app=$APP_NAME" --format '{{.ID}} {{.Names}} {{.Status}}' || true
  } >> "$PODMAN_LOG"
  mapfile -t CIDS < <(podman ps -a --filter "label=ae.app=$APP_NAME" --format '{{.ID}}') || true
  for cid in "${CIDS[@]}"; do
    [[ -z "$cid" ]] && continue
    {
      echo "===== podman logs $cid ($(date -Iseconds)) ====="
      podman logs "$cid" || true
      echo
    } >> "$OUT_DIR/${APP_NAME}-${DATE_TAG}-${cid}.log"
  done
}

# Start rollout
(
  set -x
  WAIT_READY_TRIES="${WAIT_READY_TRIES:-180}" \
  WAIT_READY_DELAY="${WAIT_READY_DELAY:-5}" \
  ./scripts/bench/run_rollout_k1s.sh \
    --label-suite "$LABEL_SUITE" \
    --app "$APP_MANIFEST" \
    --app-name "$APP_NAME" \
    --replicas "$REPLICAS" \
    --duration "$DURATION" \
    --sudo
) &>"$ROLLOUT_LOG" &
ROLLOUT_PID=$!

trap 'kill "$ROLLOUT_PID" 2>/dev/null || true' INT TERM

poll_status "$ROLLOUT_PID" &
STATUS_PID=$!
poll_events "$ROLLOUT_PID" &
EVENTS_PID=$!
poll_history "$ROLLOUT_PID" &
HISTORY_PID=$!

wait "$ROLLOUT_PID" || true
wait "$STATUS_PID" "$EVENTS_PID" "$HISTORY_PID" 2>/dev/null || true

# Final captures
python -m ae.cli status "$APP_NAME" --wide --history 50 --events >> "$STATUS_LOG" 2>&1 || true
python -m ae.cli events "$APP_NAME" --limit 100 >> "$EVENTS_LOG" 2>&1 || true
python -m ae.cli history "$APP_NAME" --limit 100 >> "$HISTORY_LOG" 2>&1 || true
collect_podman_logs

cat <<EOF3
[debug] rollout log: $ROLLOUT_LOG
[debug] status log: $STATUS_LOG
[debug] events log: $EVENTS_LOG
[debug] history log: $HISTORY_LOG
[debug] podman log: $PODMAN_LOG
EOF3

# Display tail for quick review
for f in "$ROLLOUT_LOG" "$STATUS_LOG" "$EVENTS_LOG" "$HISTORY_LOG" "$PODMAN_LOG"; do
  [[ -f "$f" ]] || continue
  echo "===== tail $f ====="
  tail -n 40 "$f"
  echo
done
