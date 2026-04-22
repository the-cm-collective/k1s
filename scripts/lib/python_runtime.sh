#!/usr/bin/env bash

k1s_find_python() {
  local root_dir="${1:?root dir required}"
  local override="${PYTHON_BIN:-}"
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi
  if [[ -x "$root_dir/.venv/bin/python" ]]; then
    printf '%s\n' "$root_dir/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  command -v python
}

k1s_ensure_runtime_libs() {
  if ! command -v nix >/dev/null 2>&1; then
    return 0
  fi
  local cc_lib
  cc_lib="$(nix eval --raw nixpkgs#stdenv.cc.cc.lib.outPath 2>/dev/null || true)"
  if [[ -z "$cc_lib" ]]; then
    return 0
  fi
  export LD_LIBRARY_PATH="$cc_lib/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

k1s_grpc_preflight() {
  local python_bin="${1:?python bin required}"
  local prefix="${2:-[k1s]}"
  local pythonpath_prefix="${3:-}"
  local output
  local rc=0

  if [[ -n "$pythonpath_prefix" ]]; then
    output="$(PYTHONPATH="${pythonpath_prefix}${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" - 2>&1 <<'PY'
import grpc
print(grpc.__version__)
PY
)" || rc=$?
  else
    output="$("$python_bin" - 2>&1 <<'PY'
import grpc
print(grpc.__version__)
PY
)" || rc=$?
  fi

  if [[ "$rc" -ne 0 ]]; then
    echo "${prefix} grpc preflight failed" >&2
    echo "$output" >&2
    echo "${prefix} ensure the repo venv is installed and libstdc++ is available via LD_LIBRARY_PATH" >&2
    return "$rc"
  fi
}
