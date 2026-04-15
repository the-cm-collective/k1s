#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"

find_python() {
  if [ -n "$PYTHON_BIN" ]; then
    printf '%s\n' "$PYTHON_BIN"
    return 0
  fi
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  command -v python
}

ensure_runtime_libs() {
  if ! command -v nix >/dev/null 2>&1; then
    return 0
  fi
  local cc_lib
  cc_lib="$(nix eval --raw nixpkgs#stdenv.cc.cc.lib.outPath 2>/dev/null || true)"
  if [ -z "$cc_lib" ]; then
    return 0
  fi
  export LD_LIBRARY_PATH="$cc_lib/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

grpc_preflight() {
  local output
  local rc=0
  output="$("$PYTHON_BIN" - 2>&1 <<'PY'
import grpc
print(grpc.__version__)
PY
)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[strict-cri-smoke] grpc preflight failed" >&2
    echo "$output" >&2
    echo "[strict-cri-smoke] ensure the repo venv is installed and libstdc++ is available via LD_LIBRARY_PATH" >&2
    return "$rc"
  fi
}

main() {
  cd "$ROOT_DIR"
  PYTHON_BIN="$(find_python)"
  if [ ! -x "$PYTHON_BIN" ]; then
    echo "[strict-cri-smoke] python not found: $PYTHON_BIN" >&2
    exit 1
  fi
  ensure_runtime_libs
  grpc_preflight
  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
  AE_STRICT_CRI_PROFILE_SMOKE="${AE_STRICT_CRI_PROFILE_SMOKE:-1}" \
    AE_CRI_IT="${AE_CRI_IT:-1}" \
    AE_CRI_SMOKE_PULL="${AE_CRI_SMOKE_PULL:-1}" \
    exec "$PYTHON_BIN" -m pytest --maxfail=1 --disable-warnings -q \
      tests/integration/test_strict_cri_profile_smoke.py \
      tests/integration/test_cri_smoke.py \
      tests/integration/test_cri_runtime_integration.py \
      "$@"
}

main "$@"
