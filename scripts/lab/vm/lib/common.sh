#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"

log() {
  echo "[lab-vm] $*"
}

err() {
  echo "[lab-vm] ERROR: $*" >&2
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    err "missing required command: ${cmd}"
    exit 2
  }
}

lab_python() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s' "$ROOT_DIR/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$(command -v python3)"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s' "$(command -v python)"
    return 0
  fi
  err "missing required command: python3"
  exit 2
}

default_run_id() {
  date -u +%Y%m%dT%H%M%SZ
}

resolve_run_id() {
  local run_id="${RUN_ID:-}"
  if [[ -z "$run_id" ]]; then
    run_id="$(default_run_id)"
  fi
  printf '%s' "$run_id"
}

run_dir() {
  local run_id="$1"
  printf '%s/runs/%s' "$ROOT_DIR" "$run_id"
}

ensure_run_dir() {
  local run_id="$1"
  mkdir -p "$(run_dir "$run_id")"
}

variant_to_json() {
  local variant_path="$1"
  shift || true
  local python_bin
  python_bin="$(lab_python)"
  PYTHONPATH="$ROOT_DIR/src" "$python_bin" "$ROOT_DIR/scripts/lab/vm/lib/variant.py" --variant "$variant_path" --print-json "$@"
}

variant_value() {
  local variant_path="$1"
  local jq_expr="$2"
  variant_to_json "$variant_path" | jq -r "$jq_expr"
}

ensure_ssh_key() {
  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  if [[ ! -f "$key_path" ]]; then
    err "ssh key not found at ${key_path}; set SSH_KEY_PATH"
    exit 2
  fi
  if [[ ! -f "${key_path}.pub" ]]; then
    err "ssh public key not found at ${key_path}.pub"
    exit 2
  fi
}

with_repo_host_mount() {
  local ip="$1"
  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$key_path" "ae@${ip}" \
    "sudo mkdir -p /mnt/host && sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true"
}

wait_for_ssh() {
  local ip="$1"
  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  local attempts="${2:-120}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$key_path" "ae@${ip}" "echo up" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

run_remote() {
  local ip="$1"
  shift
  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$key_path" "ae@${ip}" "$@"
}
