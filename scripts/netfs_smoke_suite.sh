#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[netfs-suite] %s\n' "$1"
}

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODES=("$@")
if [[ ${#MODES[@]} -eq 0 ]]; then
  if [[ -n "${NETFS_SUITE_MODES:-}" ]]; then
    # shellcheck disable=SC2206
    MODES=(${NETFS_SUITE_MODES})
  else
    MODES=(smoke snapshot csi)
  fi
fi

for mode in "${MODES[@]}"; do
  case "$mode" in
    smoke|snapshot|csi)
      log "running netfs harness mode=${mode}"
      NETFS_HARNESS_MODE="${mode}" "${ROOT_DIR}/scripts/netfs_nfs_harness.sh"
      ;;
    *)
      echo "unknown mode: ${mode} (expected smoke|snapshot|csi)" >&2
      exit 2
      ;;
  esac
done
