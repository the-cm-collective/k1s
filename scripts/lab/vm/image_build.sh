#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEMPLATE="${TEMPLATE:-$ROOT_DIR/lab/packer/ubuntu-22.04-ga.pkr.hcl}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/artifacts/images}"
VARIANT="${VARIANT:-all}"
BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"
EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"
SEED_BUNDLE_SCRIPT="${SEED_BUNDLE_SCRIPT:-$ROOT_DIR/scripts/lab/vm/image_seed_bundle.sh}"
SEED_MANIFEST="${SEED_MANIFEST:-$ROOT_DIR/lab/variants/cri_seed_images.lock.json}"
SEED_PROFILE="${SEED_PROFILE:-all}"
ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT="${ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/assert_image_boot_contract.sh}"
SEED_RUN_ID=""
SEED_BUNDLE=""

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
[[ -f "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" ]] || {
  echo "boot contract helper missing: $ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" >&2
  exit 2
}

mkdir -p "$OUTPUT_DIR"
packer init "$TEMPLATE" >/dev/null

prepare_seed_bundle() {
  [[ -f "$SEED_BUNDLE_SCRIPT" ]] || { echo "seed bundle builder missing: $SEED_BUNDLE_SCRIPT" >&2; exit 2; }
  [[ -f "$SEED_MANIFEST" ]] || { echo "seed manifest missing: $SEED_MANIFEST" >&2; exit 2; }

  if [[ -n "$SEED_BUNDLE" ]]; then
    return 0
  fi

  SEED_RUN_ID="image-build-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  SEED_BUNDLE="$ROOT_DIR/state/lab-vm/$SEED_RUN_ID/seeds/cri-seed-images.oci.tar"
  echo "[image-build] building shared seed bundle run_id=${SEED_RUN_ID}"
  bash "$SEED_BUNDLE_SCRIPT" \
    --run-id "$SEED_RUN_ID" \
    --manifest "$SEED_MANIFEST" \
    --profile "$SEED_PROFILE" \
    --output "$SEED_BUNDLE"
}

build_one() {
  local variant="$1"
  local build_dir="$OUTPUT_DIR/build-${variant}"
  echo "[image-build] building variant=${variant}"
  if [[ -d "$build_dir" ]]; then
    echo "[image-build] removing stale packer dir $build_dir"
    rm -rf "$build_dir"
  fi
  packer build \
    -var "variant=${variant}" \
    -var "output_dir=${OUTPUT_DIR}" \
    -var "seed_bundle=${SEED_BUNDLE}" \
    "$TEMPLATE"

  local image="$OUTPUT_DIR/ubuntu-22.04-k1s-${variant}.qcow2"
  local sha_file="${image}.sha256"
  local meta_file="${image}.meta.json"

  rm -f "$sha_file" "$meta_file"

  if [[ ! -f "$image" ]]; then
    echo "expected image not produced: $image" >&2
    exit 1
  fi

  echo "[image-build] verifying boot contract: $image"
  bash "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" "$image"

  sha256sum "$image" > "$sha_file"
  jq -n \
    --arg image "$(basename "$image")" \
    --arg variant "$variant" \
    --arg kernel_track "ga-5.15" \
    --arg checksum "$(cut -d' ' -f1 "$sha_file")" \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg bootstrap_contract_version "$BOOTSTRAP_CONTRACT_VERSION" \
    --arg cni_version "$EXPECTED_CNI_VERSION" \
    '{
      image:$image,
      variant:$variant,
      kernel_track:$kernel_track,
      checksum:$checksum,
      created_at:$created_at,
      bootstrap_contract_version:$bootstrap_contract_version,
      cni_version:$cni_version,
      vm_bootstrap_ready:true,
      python_alias:true,
      crictl_ready:true,
      cni_ready:true
    }' \
    > "$meta_file"

  echo "[image-build] wrote $image"
}

prepare_seed_bundle

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
