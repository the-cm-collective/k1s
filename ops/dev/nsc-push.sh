#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
NSC_BIN="${NSC_BIN:-}"

STATE_DIR="${STATE_DIR:-$ROOT_DIR/.local/nats-jwt}"
NSC_DIR="${NSC_DIR:-$STATE_DIR/nsc}"
OPERATOR="${OPERATOR:-K1S_DEV}"
ACCOUNT="${ACCOUNT:-K1S}"
SYS_ACCOUNT="${SYS_ACCOUNT:-SYS}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
SYS_CREDS="${SYS_CREDS:-$STATE_DIR/creds/sys.creds}"
PUSH_ALL="${PUSH_ALL:-0}"

if [ -z "$NSC_BIN" ] && [ -x "$ROOT_DIR/ops/dev/bin/nsc" ]; then
  NSC_BIN="$ROOT_DIR/ops/dev/bin/nsc"
fi
if [ -z "$NSC_BIN" ]; then
  NSC_BIN="$(command -v nsc || true)"
fi
if [ -z "$NSC_BIN" ] || [ ! -x "$NSC_BIN" ]; then
  echo "nsc not found; run ops/dev/install-nsc.sh or set NSC_BIN." >&2
  exit 1
fi

if [ ! -d "$NSC_DIR" ]; then
  echo "nsc directory not found: $NSC_DIR" >&2
  echo "Run ops/dev/nsc-bootstrap.sh first (or set NSC_DIR)." >&2
  exit 1
fi

if [ ! -f "$SYS_CREDS" ]; then
  echo "sys creds not found: $SYS_CREDS" >&2
  echo "Set SYS_CREDS or generate creds via ops/dev/nsc-bootstrap.sh." >&2
  exit 1
fi

HELP="$("$NSC_BIN" push --help 2>&1 || true)"

SERVER_FLAG="-u"
if echo "$HELP" | grep -q -- "--server"; then
  SERVER_FLAG="--server"
elif echo "$HELP" | grep -q -- "--url"; then
  SERVER_FLAG="--url"
elif ! echo "$HELP" | grep -q -- "-u,"; then
  SERVER_FLAG=""
fi

OPERATOR_FLAG="--operator"
if ! echo "$HELP" | grep -q -- "--operator"; then
  OPERATOR_FLAG="-o"
fi

ACCOUNT_FLAG="--account"
if ! echo "$HELP" | grep -q -- "--account"; then
  ACCOUNT_FLAG="-a"
fi

CREDS_FLAG=""
if echo "$HELP" | grep -q -- "--sys-creds"; then
  CREDS_FLAG="--sys-creds"
elif echo "$HELP" | grep -q -- "--creds"; then
  CREDS_FLAG="--creds"
fi

DIR_FLAG=""
if echo "$HELP" | grep -q -- "--all-dirs"; then
  DIR_FLAG="--all-dirs"
elif echo "$HELP" | grep -q -- "--dir"; then
  DIR_FLAG="--dir"
else
  export NSC_HOME="$NSC_DIR"
fi

cmd=("$NSC_BIN" push)
if [ -n "$SERVER_FLAG" ]; then
  cmd+=("$SERVER_FLAG" "$NATS_URL")
fi
if [ -n "$DIR_FLAG" ]; then
  cmd+=("$DIR_FLAG" "$NSC_DIR")
fi
if [ -n "$OPERATOR_FLAG" ]; then
  cmd+=("$OPERATOR_FLAG" "$OPERATOR")
fi
if [ -n "$CREDS_FLAG" ]; then
  cmd+=("$CREDS_FLAG" "$SYS_CREDS")
fi

if [ "$PUSH_ALL" = "1" ]; then
  if echo "$HELP" | grep -q -- "--all"; then
    cmd+=("--all")
  fi
else
  if [ -n "$ACCOUNT_FLAG" ]; then
    cmd+=("$ACCOUNT_FLAG" "$ACCOUNT")
  fi
fi

echo "Pushing account JWTs to $NATS_URL"
echo "  operator=$OPERATOR account=$ACCOUNT sys_account=$SYS_ACCOUNT"
printf '  cmd: %q ' "${cmd[@]}"
echo

if ! "${cmd[@]}"; then
  echo "nsc push failed. Run 'nsc push --help' to verify flags for your nsc version." >&2
  exit 1
fi
