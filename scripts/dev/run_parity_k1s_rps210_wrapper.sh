#!/usr/bin/env bash
set -euo pipefail

: "${ROOT:?export ROOT first}"
: "${RUN_ID:?export RUN_ID first}"

export AE_ETCD_MAINTENANCE_THRESHOLD_PCT="${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-60}"
export CHECKPOINT_BELL=1
export CHECKPOINT_BELL_COUNT=3
export GATING_CONCURRENCIES_CSV="${GATING_CONCURRENCIES_CSV:-30,50}"
export EXPLORATORY_CONCURRENCIES_CSV="${EXPLORATORY_CONCURRENCIES_CSV:-70}"

failures=0
sudo_keepalive_pid=""
archive_root="${ARCHIVE_ROOT:-$HOME/k1s-run-archive/$RUN_ID}"
watch_pid=""

start_sudo_keepalive() {
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[wrapper] WARN: sudo not found; skipping sudo keepalive"
    return 0
  fi
  if ! sudo -v; then
    echo "[wrapper] WARN: sudo -v failed; continuing without keepalive"
    return 0
  fi
  (
    while true; do
      sudo -n true >/dev/null 2>&1 || break
      sleep 60
    done
  ) &
  sudo_keepalive_pid="$!"
  echo "[wrapper] sudo keepalive started pid=${sudo_keepalive_pid}"
}

stop_sudo_keepalive() {
  if [[ -n "${sudo_keepalive_pid}" ]]; then
    kill "${sudo_keepalive_pid}" >/dev/null 2>&1 || true
    wait "${sudo_keepalive_pid}" >/dev/null 2>&1 || true
    echo "[wrapper] sudo keepalive stopped"
  fi
}

start_root_watch() {
  mkdir -p "$archive_root"
  if ! command -v inotifywait >/dev/null 2>&1; then
    return 0
  fi
  (
    inotifywait -m -r -e create -e delete -e delete_self -e move -e move_self "$ROOT" \
      >"$archive_root/root-watch.log" 2>&1
  ) &
  watch_pid="$!"
  echo "[wrapper] root watch started pid=${watch_pid}"
}

stop_root_watch() {
  if [[ -n "${watch_pid}" ]]; then
    kill "${watch_pid}" >/dev/null 2>&1 || true
    wait "${watch_pid}" >/dev/null 2>&1 || true
    echo "[wrapper] root watch stopped"
  fi
}

sync_archive() {
  mkdir -p "$archive_root"
  if [[ -d "$ROOT" ]]; then
    mkdir -p "$archive_root/parity-root"
    cp -a "$ROOT/." "$archive_root/parity-root/" 2>/dev/null || true
  else
    echo "[wrapper] WARN: cannot sync archive, ROOT missing: $ROOT"
  fi
  cp -a "$PWD"/state/test-results/ingress-matrix-*.json "$archive_root/" 2>/dev/null || true
}

copy_expected_jsons() {
  local run_stamp_base="$1"
  local conc_csv="$2"
  local conc

  IFS=',' read -r -a __conc_list <<<"$conc_csv"
  for conc in "${__conc_list[@]}"; do
    conc="${conc//[[:space:]]/}"
    [[ -z "$conc" ]] && continue
    local f="$ROOT/k1s/ingress-matrix-${run_stamp_base}-c${conc}.json"
    if [[ -f "$f" ]]; then
      mkdir -p "$archive_root/k1s"
      cp -a "$f" "$archive_root/k1s/" 2>/dev/null || true
    fi
  done
}

on_exit() {
  local rc=$?
  if [[ ! -d "$ROOT" ]]; then
    echo "[wrapper] WARN: ROOT missing at exit: $ROOT"
  fi
  sync_archive
  stop_root_watch
  stop_sudo_keepalive
  return "$rc"
}
trap on_exit EXIT

echo "[wrapper] ROOT=$ROOT"
echo "[wrapper] RUN_ID=$RUN_ID"
echo "[wrapper] AE_ETCD_MAINTENANCE_THRESHOLD_PCT=$AE_ETCD_MAINTENANCE_THRESHOLD_PCT"
echo "[wrapper] GATING_CONCURRENCIES_CSV=$GATING_CONCURRENCIES_CSV"
echo "[wrapper] EXPLORATORY_CONCURRENCIES_CSV=$EXPLORATORY_CONCURRENCIES_CSV"
echo "[wrapper] ARCHIVE_ROOT=$archive_root"

mkdir -p "$ROOT/k1s" "$ROOT/summary" "$ROOT/env" "$archive_root"
start_root_watch
sync_archive

# 4x additional core-proxy runs at PERF_MIN_RPS=210
for n in 1 2 3 4; do
  echo "[wrapper] ===== core-proxy r${n}/4 ====="
  scripts/dev/etcd_maintenance.sh status || true

  # Gating run: affects failure count.
  RESULTS_DIR="$ROOT/k1s" \
  RUN_STAMP_BASE="${RUN_ID}-k1s-rps210-r${n}-gate" \
  MODES=core-proxy \
  CORE_INGRESS_URL="http://127.0.0.1:10080/" \
  CORE_INGRESS_TLS_URL="https://127.0.0.1:10443/" \
  CORE_PUBLIC_INGRESS_URL="https://127.0.0.1:10443/" \
  CONCURRENCIES_CSV="$GATING_CONCURRENCIES_CSV" \
  PERF_MIN_RPS=210 \
  PERF_MAX_P95_MS=300 \
  PERF_MAX_P99_MS=500 \
  PERF_MAX_ERROR_RATE=0.01 \
  WS_MIN_CONNECTED_RATIO=1 \
  WS_MAX_CONNECT_FAILURE_RATE=0 \
  WS_MAX_MESSAGE_LOSS=0 \
  scripts/dev/run_ingress_kpi_minimatrix.sh || failures=$((failures+1))
  copy_expected_jsons "${RUN_ID}-k1s-rps210-r${n}-gate" "$GATING_CONCURRENCIES_CSV"
  sync_archive

  # Exploratory run: does not affect failure count.
  if [[ -n "${EXPLORATORY_CONCURRENCIES_CSV}" ]]; then
    RESULTS_DIR="$ROOT/k1s" \
    RUN_STAMP_BASE="${RUN_ID}-k1s-rps210-r${n}-x" \
    MODES=core-proxy \
    CORE_INGRESS_URL="http://127.0.0.1:10080/" \
    CORE_INGRESS_TLS_URL="https://127.0.0.1:10443/" \
    CORE_PUBLIC_INGRESS_URL="https://127.0.0.1:10443/" \
    CONCURRENCIES_CSV="$EXPLORATORY_CONCURRENCIES_CSV" \
    PERF_MIN_RPS=210 \
    PERF_MAX_P95_MS=300 \
    PERF_MAX_P99_MS=500 \
    PERF_MAX_ERROR_RATE=0.01 \
    WS_MIN_CONNECTED_RATIO=1 \
    WS_MAX_CONNECT_FAILURE_RATE=0 \
    WS_MAX_MESSAGE_LOSS=0 \
    scripts/dev/run_ingress_kpi_minimatrix.sh || true
    copy_expected_jsons "${RUN_ID}-k1s-rps210-r${n}-x" "$EXPLORATORY_CONCURRENCIES_CSV"
    sync_archive
  fi

  scripts/dev/etcd_maintenance.sh status || true
  # compact if pressure/NOSPACE appears (non-fatal)
  scripts/dev/etcd_maintenance.sh compact-defrag || true
done

echo "[wrapper] core-proxy gating failures: $failures/4"

# Staged mode-switch validation for LB observability (operator checkpoints + bell)
echo "[wrapper] ===== staged lane run (core-proxy -> edge-local) ====="
start_sudo_keepalive
CORE_PROXY_VALIDATION_PROFILE=deep+perf \
CORE_PROXY_PERF_PROFILE=sample \
CORE_PROXY_LB_PROOF_SCOPE=auto \
EDGE_LOCAL_ARCHETYPES=lb-distribution \
EDGE_LOCAL_VALIDATION_PROFILE=deep \
EDGE_LOCAL_LB_PROOF_SCOPE=edge-only \
EDGE_LOCAL_LB_SAMPLE_REQUESTS=5000 \
EDGE_LOCAL_LB_MIN_BACKENDS=2 \
EDGE_LOCAL_LB_MAX_SKEW_RATIO=0.35 \
PERF_MIN_RPS=210 \
PERF_MAX_P95_MS=300 \
PERF_MAX_P99_MS=500 \
PERF_MAX_ERROR_RATE=0.01 \
scripts/dev/run_ingress_mode_lanes.sh \
  --lanes core-proxy,edge-local \
  --precheck-retries 1 \
  --lane-retries 1
sync_archive

# terminal bell on wrapper completion
printf '\a'
echo "[wrapper] done"
