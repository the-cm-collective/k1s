#!/usr/bin/env bash
set -euo pipefail

# Simple helper to bootstrap a local k3s cluster via k3d for labs.
# Usage: scripts/lab_k3d.sh up|down|ensure [--name k1s-labs] [--http 8081] [--https 8444]

NAME="k1s-labs"
HTTP_PORT=8081
HTTPS_PORT=8444

while [[ $# -gt 0 ]]; do
  case "$1" in
    up|down|ensure) CMD="$1"; shift ;;
    --name) NAME="$2"; shift 2 ;;
    --http) HTTP_PORT="$2"; shift 2 ;;
    --https) HTTPS_PORT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! command -v k3d >/dev/null 2>&1; then
  echo "k3d not found. Install from https://k3d.io or 'brew install k3d' (macOS)." >&2
  exit 1
fi

exists() {
  k3d cluster list -o json | grep -q '"name":\s*"'"$NAME"'"' || return 1
}

up() {
  if exists; then
    echo "cluster '$NAME' already exists" >&2
    exit 0
  fi
  set -x
  k3d cluster create "$NAME" \
    --port "${HTTP_PORT}:80@loadbalancer" \
    --port "${HTTPS_PORT}:443@loadbalancer" \
    --wait --timeout 120s
  set +x
  echo "cluster '$NAME' ready; LB ports: http=${HTTP_PORT} https=${HTTPS_PORT}" >&2
}

down() {
  if ! exists; then
    echo "cluster '$NAME' not found" >&2
    exit 0
  fi
  set -x
  k3d cluster delete "$NAME"
  set +x
}

ensure() {
  if exists; then
    echo "cluster '$NAME' already exists" >&2
  else
    up
  fi
}

case "${CMD:-ensure}" in
  up) up ;;
  down) down ;;
  ensure) ensure ;;
  *) echo "usage: $0 up|down|ensure [--name NAME] [--http PORT] [--https PORT]" >&2; exit 2 ;;
esac

