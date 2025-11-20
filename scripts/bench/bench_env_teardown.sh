#!/usr/bin/env bash
set -euo pipefail

env_file="state/bench-env/env.sh"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      env_file="$2"; shift 2;;
    -h|--help)
      echo "Usage: scripts/bench/bench_env_teardown.sh [--env env_file]";
      exit 0;;
    *)
      echo "[bench-env] unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -f "$env_file" ]]; then
  exit 0
fi

# shellcheck disable=SC1090
source "$env_file"

if [[ -n "${BENCH_CONTROLLER_PID:-}" ]]; then
  if kill -0 "$BENCH_CONTROLLER_PID" 2>/dev/null; then
    kill "$BENCH_CONTROLLER_PID" 2>/dev/null || true
    wait "$BENCH_CONTROLLER_PID" 2>/dev/null || true
  fi
fi
if [[ -n "${BENCH_CONTROLLER_PID_FILE:-}" ]]; then
  rm -f "$BENCH_CONTROLLER_PID_FILE"
fi

if [[ "${BENCH_KEEP_ENV:-0}" != "1" ]]; then
  if [[ -n "${BENCH_ENV_DIR:-}" && -d "$BENCH_ENV_DIR" ]]; then
    rm -rf "$BENCH_ENV_DIR"
  fi
  rm -f "$env_file"
else
  echo "[bench-env] keeping env dir $BENCH_ENV_DIR (BENCH_KEEP_ENV=1)" >&2
  echo "[bench-env] keeping env file $env_file" >&2
fi
