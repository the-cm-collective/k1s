#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: scripts/containerd_socket_access.sh [--grant|--revoke] [options]

Options:
  --grant               Grant user access to the containerd socket (ACL)
  --revoke              Restore previous ACLs from the record file
  --socket <path>       Socket path (default: derived from AE_CRI_ENDPOINT)
  --endpoint <endpoint> CRI endpoint (e.g., unix:///run/containerd/containerd.sock)
  --user <name>         Target user (default: SUDO_USER or USER)
  --record <path>       ACL record path (default: state/containerd.sock.acl)
  -h, --help            Show this help
USAGE
}

action=""
socket=""
endpoint=""
user=""
record=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --grant)
      action="grant"
      ;;
    --revoke)
      action="revoke"
      ;;
    --socket)
      socket="${2:-}"
      shift
      ;;
    --endpoint)
      endpoint="${2:-}"
      shift
      ;;
    --user)
      user="${2:-}"
      shift
      ;;
    --record)
      record="${2:-}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ -z "$action" ]]; then
  echo "Missing --grant or --revoke" >&2
  usage
  exit 1
fi

if [[ -z "$user" ]]; then
  user="${SUDO_USER:-${USER:-}}"
fi
if [[ -z "$user" ]]; then
  echo "Unable to determine target user" >&2
  exit 1
fi

if [[ -z "$record" ]]; then
  record="${ROOT_DIR}/state/containerd.sock.acl"
fi

if [[ -z "$socket" ]]; then
  endpoint="${endpoint:-${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}}"
  if [[ "$endpoint" == unix://* ]]; then
    socket="${endpoint#unix://}"
  else
    socket="$endpoint"
  fi
fi

if [[ -z "$socket" || "$socket" != /* ]]; then
  echo "Invalid socket path: ${socket:-<empty>}" >&2
  exit 1
fi

if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl not found; install acl package to manage socket permissions" >&2
  exit 1
fi
if ! command -v getfacl >/dev/null 2>&1; then
  echo "getfacl not found; install acl package to manage socket permissions" >&2
  exit 1
fi

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
fi

case "$action" in
  grant)
    mkdir -p "$(dirname "$record")"
    $SUDO getfacl -p "$socket" >"$record"
    $SUDO setfacl -m "u:${user}:rw" "$socket"
    echo "Granted ${user} access to ${socket} (record: ${record})"
    ;;
  revoke)
    if [[ ! -f "$record" ]]; then
      echo "No record file found at ${record}; nothing to revoke" >&2
      exit 0
    fi
    $SUDO setfacl --restore "$record"
    rm -f "$record"
    echo "Restored ACLs for ${socket}"
    ;;
  *)
    echo "Unknown action: $action" >&2
    exit 1
    ;;
esac
