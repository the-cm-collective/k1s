#!/usr/bin/env bash
set -euo pipefail

# Tiny helper for OCI conversions with Podman/Skopeo semantics.
#
# Usage:
#   scripts/oci_convert.sh image <image-ref> <oci-archive.tar>|<oci-dir>
#   scripts/oci_convert.sh compose -f <docker-compose.yaml> [-o output.yaml]
#
# Notes:
# - Uses `podman image save --format oci-archive` for archives, and `--format oci-dir`
#   for directories. Requires Podman.
# - For compose, this emits Podman's resolved config via `podman compose config`.
#   Most docker-compose files work as-is with Podman. Removing the `version:` key
#   is recommended to silence deprecation warnings.

usage() {
  cat <<USAGE
Usage:
  $0 image <image-ref> <oci-archive.tar>|<oci-dir>
  $0 compose -f <docker-compose.yaml> [-o output.yaml]
USAGE
}

cmd=${1:-}
shift || true

case "$cmd" in
  image)
    img=${1:-}
    out=${2:-}
    if [[ -z "$img" || -z "$out" ]]; then
      usage; exit 2
    fi
    if ! command -v podman >/dev/null 2>&1; then
      echo "podman not found; install Podman to use image export" >&2
      exit 1
    fi
    if [[ "$out" == *.tar || "$out" == *.oci || "$out" == *.tar.gz ]]; then
      # Archive export
      fmt=oci-archive
      [[ "$out" == *.tar.gz ]] && echo "Note: podman does not gzip oci-archive; gzip manually if needed" >&2
      podman image save --format "$fmt" -o "$out" "$img"
    else
      # Directory export
      fmt=oci-dir
      mkdir -p "$out"
      podman image save --format "$fmt" -o "$out" "$img"
    fi
    ;;
  compose)
    file=
    out=
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -f) file=${2:?}; shift ;;
        -o) out=${2:?}; shift ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
      esac
      shift
    done
    if [[ -z "$file" ]]; then
      usage; exit 2
    fi
    if ! command -v podman >/dev/null 2>&1; then
      echo "podman not found; install Podman to use compose conversion" >&2
      exit 1
    fi
    if [[ -z "$out" ]]; then
      podman compose -f "$file" config
    else
      podman compose -f "$file" config > "$out"
      echo "Wrote Podman-resolved compose to $out"
    fi
    ;;
  *)
    usage; exit 2 ;;
esac

