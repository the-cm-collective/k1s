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
    echo "[strict-cri-smoke] python not found: $PYTHON_BIN" >&2
    exit 1
  fi
  k1s_ensure_runtime_libs
  k1s_grpc_preflight "$PYTHON_BIN" "[strict-cri-smoke]"
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
