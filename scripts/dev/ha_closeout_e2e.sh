#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
source "${ROOT_DIR}/scripts/lib/python_runtime.sh"
PYTHON_BIN="${PYTHON_BIN:-}"

main() {
  cd "$ROOT_DIR"
  PYTHON_BIN="$(k1s_find_python "$ROOT_DIR")"
  if [ ! -x "$PYTHON_BIN" ]; then
    echo "[ha-closeout-e2e] python not found: $PYTHON_BIN" >&2
    exit 1
  fi
  k1s_ensure_runtime_libs
  k1s_grpc_preflight "$PYTHON_BIN" "[ha-closeout-e2e]"
  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
  AE_E2E_HA_CLOSEOUT=1 exec "$PYTHON_BIN" -m pytest -q tests/integration/test_ha_closeout_e2e.py "$@"
}

main "$@"
