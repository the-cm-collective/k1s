#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

INSTALL_ROOT="${NSC_INSTALL_ROOT:-$ROOT_DIR/ops/dev/bin}"
NSC_HOME="${NSC_HOME:-$ROOT_DIR/.local/nsc-home}"
NSC_VERSION="${NSC_VERSION:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
INSTALL_PY_URL="https://raw.githubusercontent.com/nats-io/nsc/master/install.py"

mkdir -p "$INSTALL_ROOT" "$NSC_HOME"

if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "python3/python not found; install Python to run the official nsc installer." >&2
    exit 1
  fi
fi

echo "Installing nsc via official install.py into $NSC_HOME/.nsccli/bin"
if [ -n "$NSC_VERSION" ]; then
  curl -fsSL "$INSTALL_PY_URL" | HOME="$NSC_HOME" "$PYTHON_BIN" - "$NSC_VERSION"
else
  curl -fsSL "$INSTALL_PY_URL" | HOME="$NSC_HOME" "$PYTHON_BIN" -
fi

SRC="$NSC_HOME/.nsccli/bin/nsc"
if [ -f "$NSC_HOME/.nsccli/bin/nsc.exe" ]; then
  SRC="$NSC_HOME/.nsccli/bin/nsc.exe"
fi

if [ ! -f "$SRC" ]; then
  echo "nsc install did not produce a binary at $SRC" >&2
  exit 1
fi

DEST="$INSTALL_ROOT/$(basename "$SRC")"
cp "$SRC" "$DEST"
chmod +x "$DEST"

echo "nsc installed at $DEST"
echo "Add to PATH or set NSC_BIN=$DEST"
