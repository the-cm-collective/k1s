#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-ops/dev/docker-compose.nats-etcd.yaml}"
METRICS_PORT="${METRICS_PORT:-19118}"
SITE_ID="${SITE_ID:-sfo-edge-01}"
JS_DOMAIN="${AE_JS_DOMAIN:-K1S}"
NODE_ID="${NODE_ID:-node-01}"
GATEWAY_ID="${GATEWAY_ID:-gateway-01}"
CLEANUP="${CLEANUP:-1}"
LOG_DIR="${LOG_DIR:-}"
PYTHON_BIN="${PYTHON_BIN:-}"

if [ -z "$PYTHON_BIN" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python"
  fi
fi

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

setup_dirs() {
  TMPDIR=$(mktemp -d)
  if [ -z "$LOG_DIR" ]; then
    LOG_DIR="$TMPDIR/logs"
  fi
  mkdir -p "$LOG_DIR"
}

cleanup() {
  set +e
  if [ -n "${TMPDIR:-}" ]; then
    if [ -f "$TMPDIR/controller.pid" ]; then kill "$(cat "$TMPDIR/controller.pid")" >/dev/null 2>&1 || true; fi
    if [ -f "$TMPDIR/gateway.pid" ]; then kill "$(cat "$TMPDIR/gateway.pid")" >/dev/null 2>&1 || true; fi
    if [ -f "$TMPDIR/worker.pid" ]; then kill "$(cat "$TMPDIR/worker.pid")" >/dev/null 2>&1 || true; fi
  fi
  if [ "$CLEANUP" = "1" ]; then
    compose down >/dev/null 2>&1 || true
  fi
}

wait_port() {
  host="$1"; port="$2"; timeout="$3"
  "$PYTHON_BIN" - <<PY
import socket, time
host="$host"; port=int("$port"); timeout=float("$timeout")
end=time.time()+timeout
while time.time() < end:
    try:
        with socket.create_connection((host, port), timeout=1):
            print("ready")
            raise SystemExit(0)
    except OSError:
        time.sleep(0.5)
raise SystemExit(f"timeout waiting for {host}:{port}")
PY
}

start_controller() {
  backend="$1"
  PYTHONPATH=src \
  AE_STATE_DB="$TMPDIR/state.db" \
  AE_PROJECTION_ROOT="$TMPDIR/projections" \
  AE_TRANSPORT_BACKEND="$backend" \
  AE_SITE_IDS="$SITE_ID" \
  AE_NATS_URL=nats://hub-controller:dev@127.0.0.1:4222 \
  AE_JS_DOMAIN="$JS_DOMAIN" \
  AE_SITE_NOTREADY_AFTER=6 \
  "$PYTHON_BIN" -m ae.controller --loop --interval 2 --metrics-port "$METRICS_PORT" \
    >"$LOG_DIR/controller-$backend.log" 2>&1 &
  echo $! > "$TMPDIR/controller.pid"
}

stop_controller() {
  if [ -f "$TMPDIR/controller.pid" ]; then
    kill "$(cat "$TMPDIR/controller.pid")" >/dev/null 2>&1 || true
    rm -f "$TMPDIR/controller.pid"
  fi
}

start_gateway() {
  PYTHONPATH=src \
  AE_TRANSPORT_BACKEND=nats-js \
  AE_SITE_ID="$SITE_ID" \
  AE_NATS_URL=nats://gateway:dev@127.0.0.1:4223 \
  AE_JS_DOMAIN="$JS_DOMAIN" \
  AE_NODE_ID="$GATEWAY_ID" \
  AE_GATEWAY_SPOOL_PATH="$TMPDIR/gateway-spool.db" \
  AE_GATEWAY_STATUS_PUBLISH_INTERVAL=2 \
  AE_GATEWAY_LOGS_PUBLISH_INTERVAL=2 \
  AE_GATEWAY_WORK_HEARTBEAT_TIMEOUT=4 \
  AE_GATEWAY_WORK_NAK_DELAY=1 \
  "$PYTHON_BIN" -m ae.gateway \
    >"$LOG_DIR/gateway.log" 2>&1 &
  echo $! > "$TMPDIR/gateway.pid"
}

stop_gateway() {
  if [ -f "$TMPDIR/gateway.pid" ]; then
    kill "$(cat "$TMPDIR/gateway.pid")" >/dev/null 2>&1 || true
    rm -f "$TMPDIR/gateway.pid"
  fi
}

start_worker() {
  delay_ms="$1"
  progress="$2"
  PYTHONPATH=src \
  "$PYTHON_BIN" -m ae.worker_stub \
    --node-id "$NODE_ID" \
    --nats-url nats://worker:dev@127.0.0.1:4223 \
    --delay-ms "$delay_ms" \
    --progress-interval "$progress" \
    >"$LOG_DIR/worker.log" 2>&1 &
  echo $! > "$TMPDIR/worker.pid"
}

stop_worker() {
  if [ -f "$TMPDIR/worker.pid" ]; then
    kill "$(cat "$TMPDIR/worker.pid")" >/dev/null 2>&1 || true
    rm -f "$TMPDIR/worker.pid"
  fi
}

metric() {
  name="$1"; label="${2:-}"
  "$PYTHON_BIN" - "$name" "$label" <<PY
import sys, urllib.request
name = sys.argv[1]
label = sys.argv[2] if len(sys.argv) > 2 else ""
text = urllib.request.urlopen("http://127.0.0.1:${METRICS_PORT}/metrics").read().decode()
for line in text.splitlines():
    if not line.startswith(name):
        continue
    if label and label not in line:
        continue
    parts = line.split()
    if len(parts) >= 2:
        print(parts[1])
        sys.exit(0)
print("")
PY
}

wait_metrics_ready() {
  "$PYTHON_BIN" - <<PY
import time, urllib.request
end=time.time()+30
while time.time()<end:
    try:
        urllib.request.urlopen("http://127.0.0.1:${METRICS_PORT}/metrics").read()
        print("metrics_ready")
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("metrics not ready")
PY
}

wait_site_seen() {
  "$PYTHON_BIN" - <<PY
import time, urllib.request
end=time.time()+30
while time.time()<end:
    text = urllib.request.urlopen("http://127.0.0.1:${METRICS_PORT}/metrics").read().decode()
    if "ae_site_last_seen_seconds" in text:
        print("site_seen")
        break
    time.sleep(1)
else:
    raise SystemExit("site telemetry not seen")
PY
}

canary() {
  start_controller "nats-js"
  start_gateway
  start_worker 50 5
  wait_metrics_ready
  wait_site_seen
  start_val=$(metric ae_outbox_publish_success_total)
  PYTHONPATH=src AE_STATE_DB="$TMPDIR/state.db" "$PYTHON_BIN" -m ae.cli work enqueue --site-id "$SITE_ID" --mode outbox --op ensure_pod --preferred-node "$NODE_ID" >/dev/null
  for _ in $(seq 1 20); do
    val=$(metric ae_outbox_publish_success_total || true)
    if [ -n "$val" ]; then
      "$PYTHON_BIN" - <<PY
import sys
start=float("$start_val")
val=float("$val")
if val-start >= 1:
    sys.exit(0)
PY
      if [ $? -eq 0 ]; then
        echo "canary:ok"
        return 0
      fi
    fi
    sleep 1
  done
  echo "canary:fail"
  return 1
}

rollback() {
  stop_gateway
  stop_worker
  stop_controller
  start_controller "http"
  wait_metrics_ready
  echo "rollback:ok"
}

main() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found" >&2
    exit 1
  fi
  setup_dirs
  trap cleanup EXIT
  compose up -d >/dev/null
  wait_port 127.0.0.1 4222 20
  wait_port 127.0.0.1 4223 20
  canary
  rollback
}

main "$@"
