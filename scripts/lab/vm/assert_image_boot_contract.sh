#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/image_boot_contract.sh
source "$SCRIPT_DIR/lib/image_boot_contract.sh"

usage() {
  cat <<USAGE
Usage: $0 <qcow-image>
USAGE
}

main() {
  local image="${1:-}"
  [[ -n "$image" ]] || {
    usage >&2
    exit 2
  }
  [[ $# -eq 1 ]] || {
    usage >&2
    exit 2
  }

  trap boot_contract_cleanup EXIT
  boot_contract_open_image "$image"

  if ! boot_contract_assert; then
    boot_contract_print_report >&2
    exit 1
  fi

  printf '[image-boot-contract] ok: %s\n' "$image"
}

main "$@"
