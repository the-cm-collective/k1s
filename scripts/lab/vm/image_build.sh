#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEMPLATE="${TEMPLATE:-$ROOT_DIR/lab/packer/ubuntu-22.04-ga.pkr.hcl}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/artifacts/images}"
VARIANT="${VARIANT:-all}"

usage() {
  cat <<USAGE
Usage: $0 [--variant base|gpu|all] [--output-dir path] [--template path]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

command -v packer >/dev/null 2>&1 || { echo "packer missing" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq missing" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum missing" >&2; exit 2; }

mkdir -p "$OUTPUT_DIR"
packer init "$TEMPLATE" >/dev/null

build_one() {
  local variant="$1"
  echo "[image-build] building variant=${variant}"
  packer build \
    -var "variant=${variant}" \
    -var "output_dir=${OUTPUT_DIR}" \
    "$TEMPLATE"

  local image="$OUTPUT_DIR/ubuntu-22.04-k1s-${variant}.qcow2"
  local sha_file="${image}.sha256"
  local meta_file="${image}.meta.json"

  if [[ ! -f "$image" ]]; then
    echo "expected image not produced: $image" >&2
    exit 1
  fi

  sha256sum "$image" > "$sha_file"
  jq -n \
    --arg image "$(basename "$image")" \
    --arg variant "$variant" \
    --arg kernel_track "ga-5.15" \
    --arg checksum "$(cut -d' ' -f1 "$sha_file")" \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      image:$image,
      variant:$variant,
      kernel_track:$kernel_track,
      checksum:$checksum,
      created_at:$created_at,
      vm_bootstrap_ready:true,
      python_alias:true,
      crictl_ready:true,
      cni_ready:true
    }' \
    > "$meta_file"

  echo "[image-build] wrote $image"
}

case "$VARIANT" in
  base) build_one base ;;
  gpu) build_one gpu ;;
  all)
    build_one base
    build_one gpu
    ;;
  *)
    echo "invalid variant: $VARIANT" >&2
    exit 2
    ;;
esac

echo "[image-build] complete: $OUTPUT_DIR"
