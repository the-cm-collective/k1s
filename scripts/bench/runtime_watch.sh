#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${AE_BENCH_WATCH_APP:-echo}"
INTERVAL="${AE_BENCH_WATCH_INTERVAL:-10}"
OUT_DIR="${AE_BENCH_WATCH_DIR:-state/bench-env/debug}"
LOG_FILE="$OUT_DIR/podman-watch-$(date +%Y%m%d-%H%M%S).log"
EVENT_LOG="$OUT_DIR/podman-events-$(date +%Y%m%d-%H%M%S).log"
PODMAN_BIN="${AE_PODMAN_BIN:-podman}"
CONTROLLER_LOG="${AE_BENCH_CONTROLLER_LOG:-state/bench-env/controller.log}"
TAIL_LINES="${AE_BENCH_WATCH_LOG_TAIL:-40}"
CONTAINER_TAIL="${AE_BENCH_WATCH_CONTAINER_TAIL:-50}"

mkdir -p "$OUT_DIR"
if [[ ! -e "$CONTROLLER_LOG" ]]; then
  echo "warning: controller log $CONTROLLER_LOG not found; controller tail will be skipped" | tee -a "$LOG_FILE"
fi

echo "writing runtime snapshots to $LOG_FILE" | tee -a "$LOG_FILE"
if command -v "$PODMAN_BIN" >/dev/null 2>&1; then
  echo "using podman binary: $PODMAN_BIN" | tee -a "$LOG_FILE"
else
  echo "error: podman binary '$PODMAN_BIN' not found" | tee -a "$LOG_FILE"
  exit 1
fi

echo "[$(date --iso-8601=seconds)] starting podman events capture" | tee -a "$LOG_FILE"
"$PODMAN_BIN" events --filter "label=ae.app=$APP_NAME" --format '{{json .}}' >> "$EVENT_LOG" 2>&1 &
EVENT_PID=$!
trap 'kill $EVENT_PID >/dev/null 2>&1 || true' EXIT

echo "podman events stream: $EVENT_LOG" | tee -a "$LOG_FILE"

touch "$LOG_FILE"
while true; do
  ts="$(date --iso-8601=seconds)"
  {
    echo
    echo "[$ts]"
    "$PODMAN_BIN" ps -a --filter "label=ae.app=$APP_NAME" --format '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}'
  } | tee -a "$LOG_FILE"

  mapfile -t names < <("$PODMAN_BIN" ps -a --filter "label=ae.app=$APP_NAME" --format '{{.Names}}')
  for name in "${names[@]}"; do
    [[ -z "$name" ]] && continue
    echo "--- tail logs for $name ---" | tee -a "$LOG_FILE"
    "$PODMAN_BIN" logs --tail "$CONTAINER_TAIL" "$name" 2>&1 | sed 's/^/    /' | tee -a "$LOG_FILE"
    inspect_file="$OUT_DIR/inspect-$name.json"
    if [[ ! -s "$inspect_file" ]]; then
      "$PODMAN_BIN" inspect "$name" > "$inspect_file" 2>/dev/null || true
    fi
  done

  if [[ -f "$CONTROLLER_LOG" ]]; then
    echo "--- controller tail ($TAIL_LINES lines) ---" | tee -a "$LOG_FILE"
    tail -n "$TAIL_LINES" "$CONTROLLER_LOG" | sed 's/^/    /' | tee -a "$LOG_FILE"
  fi

  sleep "$INTERVAL"
done
