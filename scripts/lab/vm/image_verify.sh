#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE_DIR="${IMAGE_DIR:-$ROOT_DIR/artifacts/images}"
VARIANT="${VARIANT:-all}"
BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"
EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"

usage() {
  cat <<USAGE
Usage: $0 [--image-dir path] [--variant base|gpu|all]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-dir) IMAGE_DIR="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

command -v qemu-img >/dev/null 2>&1 || { echo "qemu-img missing" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq missing" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum missing" >&2; exit 2; }

require_meta_expr() {
  local meta_file="$1"
  local description="$2"
  shift 2
  if ! jq -e "$@" "$meta_file" >/dev/null; then
    echo "[image-verify] metadata check failed (${description}): ${meta_file}" >&2
    exit 1
  fi
}

check_one() {
  local variant="$1"
  local image="$IMAGE_DIR/ubuntu-22.04-k1s-${variant}.qcow2"
  local sha_file="${image}.sha256"
  local meta_file="${image}.meta.json"

  [[ -f "$image" ]] || { echo "missing image: $image" >&2; exit 1; }
  [[ -f "$sha_file" ]] || { echo "missing checksum: $sha_file" >&2; exit 1; }
  [[ -f "$meta_file" ]] || { echo "missing metadata: $meta_file" >&2; exit 1; }

  (cd "$IMAGE_DIR" && sha256sum -c "$(basename "$sha_file")")
  qemu-img info --output=json "$image" | jq -e '.format == "qcow2"' >/dev/null
  require_meta_expr "$meta_file" "kernel_track" '.kernel_track == "ga-5.15"'
  require_meta_expr \
    "$meta_file" \
    "bootstrap_contract_version=${BOOTSTRAP_CONTRACT_VERSION}" \
    --arg v "$BOOTSTRAP_CONTRACT_VERSION" \
    '.bootstrap_contract_version == $v'
  require_meta_expr \
    "$meta_file" \
    "cni_version=${EXPECTED_CNI_VERSION}" \
    --arg v "$EXPECTED_CNI_VERSION" \
    '.cni_version == $v'
  require_meta_expr "$meta_file" "vm_bootstrap_ready=true" '.vm_bootstrap_ready == true'
  require_meta_expr "$meta_file" "python_alias=true" '.python_alias == true'
  require_meta_expr "$meta_file" "crictl_ready=true" '.crictl_ready == true'
  require_meta_expr "$meta_file" "cni_ready=true" '.cni_ready == true'

  echo "[image-verify] ok: $image"
}

case "$VARIANT" in
  base) check_one base ;;
  gpu) check_one gpu ;;
  all)
    check_one base
    check_one gpu
    ;;
  *)
    echo "invalid variant: $VARIANT" >&2
    exit 2
    ;;
esac
