#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-ops/dev/docker-compose.nats-etcd.yaml}"
METRICS_PORT="${METRICS_PORT:-19108}"
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
  PYTHONPATH=src \
  AE_STATE_DB="$TMPDIR/state.db" \
  AE_PROJECTION_ROOT="$TMPDIR/projections" \
  AE_TRANSPORT_BACKEND=nats-js \
  AE_SITE_IDS="$SITE_ID" \
  AE_NATS_URL=nats://hub-controller:dev@127.0.0.1:4222 \
  AE_JS_DOMAIN="$JS_DOMAIN" \
  AE_SITE_NOTREADY_AFTER=6 \
  "$PYTHON_BIN" -m ae.controller --loop --interval 2 --metrics-port "$METRICS_PORT" \
    >"$LOG_DIR/controller.log" 2>&1 &
  echo $! > "$TMPDIR/controller.pid"
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

wait_metric_eq() {
  name="$1"; label="$2"; expected="$3"; timeout="$4"
  end=$((SECONDS+timeout))
  while [ $SECONDS -lt $end ]; do
    val=$(metric "$name" "$label" || true)
    if [ "$val" = "$expected" ]; then
      echo "$name=$val"
      return 0
    fi
    sleep 1
  done
  echo "$name timeout (last=$val)"
  return 1
}

wait_metric_increase() {
  name="$1"; label="$2"; start="$3"; min_delta="$4"; timeout="$5"
  end=$((SECONDS+timeout))
  while [ $SECONDS -lt $end ]; do
    val=$(metric "$name" "$label" || true)
    if [ -n "$val" ]; then
      "$PYTHON_BIN" - <<PY
import sys
start=float("$start")
val=float("$val")
if val-start >= float("$min_delta"):
    print("$name="+str(val))
    sys.exit(0)
PY
      if [ $? -eq 0 ]; then
        return 0
      fi
    fi
    sleep 1
  done
  echo "$name timeout (last=$val)"
  return 1
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

run_drills() {
  results=()

  # Drill 1: leaf disconnect
  compose stop nats-edge >/dev/null
  wait_metric_eq ae_site_stale "site=\"$SITE_ID\"" 1 20 && results+=("leaf_disconnect:ok") || results+=("leaf_disconnect:fail")

  # Drill 2: gateway reconnect
  compose start nats-edge >/dev/null
  stop_gateway
  start_gateway
  wait_metric_eq ae_site_stale "site=\"$SITE_ID\"" 0 20 && results+=("gateway_reconnect:ok") || results+=("gateway_reconnect:fail")

  # Drill 3: JS consumer lag (outbox publishes)
  start_val=$(metric ae_outbox_publish_success_total)
  for _ in $(seq 1 5); do
    PYTHONPATH=src AE_STATE_DB="$TMPDIR/state.db" "$PYTHON_BIN" -m ae.cli work enqueue --site-id "$SITE_ID" --mode outbox --op ensure_pod --preferred-node "$NODE_ID" >/dev/null
    sleep 0.2
  done
  wait_metric_increase ae_outbox_publish_success_total "" "$start_val" 5 30 && results+=("js_consumer_lag:ok") || results+=("js_consumer_lag:fail")

  # Drill 4: hub NATS restart
  compose restart nats-hub >/dev/null
  sleep 3
  start_val=$(metric ae_outbox_publish_success_total)
  PYTHONPATH=src AE_STATE_DB="$TMPDIR/state.db" "$PYTHON_BIN" -m ae.cli work enqueue --site-id "$SITE_ID" --mode outbox --op ensure_pod --preferred-node "$NODE_ID" >/dev/null
  wait_metric_increase ae_outbox_publish_success_total "" "$start_val" 1 30 && results+=("hub_restart:ok") || results+=("hub_restart:fail")

  # Drill 5: site disconnect/reconnect
  compose stop nats-edge >/dev/null
  stop_gateway
  wait_metric_eq ae_site_stale "site=\"$SITE_ID\"" 1 20 && results+=("site_disconnect:ok") || results+=("site_disconnect:fail")
  compose start nats-edge >/dev/null
  start_gateway
  wait_metric_eq ae_site_stale "site=\"$SITE_ID\"" 0 20 && results+=("site_reconnect:ok") || results+=("site_reconnect:fail")

  # Drill 6: worker crash mid-work
  stop_worker
  start_worker 15000 2
  start_stale=$(metric ae_gateway_work_stale_total "site=\"$SITE_ID\"")
  start_nak=$(metric ae_gateway_work_nak_total "site=\"$SITE_ID\"")
  PYTHONPATH=src AE_STATE_DB="$TMPDIR/state.db" "$PYTHON_BIN" -m ae.cli work enqueue --site-id "$SITE_ID" --mode outbox --op ensure_pod --preferred-node "$NODE_ID" >/dev/null
  sleep 3
  stop_worker
  wait_metric_increase ae_gateway_work_stale_total "site=\"$SITE_ID\"" "$start_stale" 1 25 && stale_ok=1 || stale_ok=0
  wait_metric_increase ae_gateway_work_nak_total "site=\"$SITE_ID\"" "$start_nak" 1 25 && nak_ok=1 || nak_ok=0
  if [ $stale_ok -eq 1 ] && [ $nak_ok -eq 1 ]; then
    results+=("worker_crash_mid_work:ok")
  else
    results+=("worker_crash_mid_work:fail")
  fi

  # Drill 7: etcd leader change (simulated by restart)
  compose restart etcd >/dev/null
  sleep 2
  "$PYTHON_BIN" - <<PY
import urllib.request
urllib.request.urlopen("http://127.0.0.1:${METRICS_PORT}/metrics").read()
print("metrics_ok")
PY
  results+=("etcd_restart:ok")

  printf '%s\n' "${results[@]}"
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
  start_controller
  start_gateway
  start_worker 50 5
  wait_metrics_ready
  wait_site_seen
  run_drills
}

main "$@"
