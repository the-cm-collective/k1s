#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE_DIR="${IMAGE_DIR:-$ROOT_DIR/artifacts/images}"
DEST_HOST=""
DEST_DIR=""
VARIANT="all"

usage() {
  cat <<USAGE
Usage: $0 --host <user@host> --dest <dir> [--image-dir path] [--variant base|gpu|all]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) DEST_HOST="$2"; shift 2 ;;
    --dest) DEST_DIR="$2"; shift 2 ;;
    --image-dir) IMAGE_DIR="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -n "$DEST_HOST" ]] || { echo "--host required" >&2; exit 2; }
[[ -n "$DEST_DIR" ]] || { echo "--dest required" >&2; exit 2; }

command -v rsync >/dev/null 2>&1 || { echo "rsync missing" >&2; exit 2; }
command -v ssh >/dev/null 2>&1 || { echo "ssh missing" >&2; exit 2; }

transfer_one() {
  local variant="$1"
  local image="$IMAGE_DIR/ubuntu-22.04-k1s-${variant}.qcow2"
  local sha_file="${image}.sha256"
  local meta_file="${image}.meta.json"

  [[ -f "$image" && -f "$sha_file" && -f "$meta_file" ]] || {
    echo "missing build artifacts for variant=${variant} under $IMAGE_DIR" >&2
    exit 1
  }

  rsync -av "$image" "$sha_file" "$meta_file" "${DEST_HOST}:${DEST_DIR}/"
  ssh "$DEST_HOST" "cd '$DEST_DIR' && sha256sum -c '$(basename "$sha_file")'"
  echo "[image-transfer] synced variant=${variant} to ${DEST_HOST}:${DEST_DIR}"
}

case "$VARIANT" in
  base) transfer_one base ;;
  gpu) transfer_one gpu ;;
  all)
    transfer_one base
    transfer_one gpu
    ;;
  *)
    echo "invalid variant: $VARIANT" >&2
    exit 2
    ;;
esac
