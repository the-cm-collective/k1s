#!/usr/bin/env bash
set -euo pipefail

echo "[dashboard] restarting supervisor (to re-source env)"

if [ -s state/controller_supervisor.pid ] && kill -0 "$(cat state/controller_supervisor.pid)" 2>/dev/null; then
  sup="$(cat state/controller_supervisor.pid)"
  echo "[dashboard] killing supervisor pid=$sup"
  kill "$sup" || true
  # wait up to ~5s for supervisor to clean up pid/lock
  for i in 1 2 3 4 5; do
    if [ ! -f state/controller_supervisor.pid ]; then break; fi
    sleep 1
  done
else
  echo "[dashboard] no supervisor process found"
fi

# if a stale lock remains but no supervisor pid, clear it
if [ -f state/controller_supervisor.lock ] && [ ! -f state/controller_supervisor.pid ]; then
  echo "[dashboard] removing stale supervisor lock"
  rm -f state/controller_supervisor.lock || true
fi

# ensure any leftover controller is gone
if [ -s state/controller.pid ] && kill -0 "$(cat state/controller.pid)" 2>/dev/null; then
  cpid="$(cat state/controller.pid)"
  echo "[dashboard] stopping orphan controller pid=$cpid"
  kill "$cpid" || true
fi

