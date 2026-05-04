#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE_DIR="${IMAGE_DIR:-$ROOT_DIR/artifacts/images}"
VARIANT="${VARIANT:-all}"
BOOTSTRAP_CONTRACT_VERSION="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"
EXPECTED_CNI_VERSION="${EXPECTED_CNI_VERSION:-0.4.0}"
METADATA_ONLY=0
PURGE_FAILED=0
VARIANT_UP_SCRIPT="${VARIANT_UP_SCRIPT:-$ROOT_DIR/scripts/lab/vm/variant_up.sh}"
VARIANT_DOWN_SCRIPT="${VARIANT_DOWN_SCRIPT:-$ROOT_DIR/scripts/lab/vm/variant_down.sh}"
VERIFY_SSH_BIN="${VERIFY_SSH_BIN:-ssh}"
INSPECT_QCOW_BOOT_SCRIPT="${INSPECT_QCOW_BOOT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/inspect_qcow_boot.sh}"
ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT="${ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT:-$ROOT_DIR/scripts/lab/vm/assert_image_boot_contract.sh}"

usage() {
  cat <<USAGE
Usage: $0 [--image-dir path] [--variant base|gpu|all] [--metadata-only] [--purge-failed]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-dir) IMAGE_DIR="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --metadata-only) METADATA_ONLY=1; shift ;;
    --purge-failed) PURGE_FAILED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

command -v qemu-img >/dev/null 2>&1 || { echo "qemu-img missing" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq missing" >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum missing" >&2; exit 2; }

if [[ "$METADATA_ONLY" -eq 0 ]]; then
  [[ -f "$VARIANT_UP_SCRIPT" ]] || { echo "variant up helper missing: $VARIANT_UP_SCRIPT" >&2; exit 2; }
  [[ -f "$VARIANT_DOWN_SCRIPT" ]] || { echo "variant down helper missing: $VARIANT_DOWN_SCRIPT" >&2; exit 2; }
  [[ -f "$INSPECT_QCOW_BOOT_SCRIPT" ]] || { echo "boot inspect helper missing: $INSPECT_QCOW_BOOT_SCRIPT" >&2; exit 2; }
  [[ -f "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" ]] || {
    echo "boot contract helper missing: $ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" >&2
    exit 2
  }
fi

require_meta_expr() {
  local meta_file="$1"
  local description="$2"
  shift 2
  if ! jq -e "$@" "$meta_file" >/dev/null; then
    echo "[image-verify] metadata check failed (${description}): ${meta_file}" >&2
    exit 1
  fi
}

image_virtual_size_bytes() {
  local image="$1"
  qemu-img info --output=json "$image" | jq -er '."virtual-size"'
}

image_virtual_size_gib() {
  local image="$1"
  local size_bytes=""
  size_bytes="$(image_virtual_size_bytes "$image")"
  [[ "$size_bytes" =~ ^[0-9]+$ ]] || {
    echo "[image-verify] invalid virtual size for image: ${image}" >&2
    exit 1
  }
  printf '%s\n' "$(((size_bytes + 1073741824 - 1) / 1073741824))"
}

verify_gpu_guest_contract() {
  local guest_ip="$1"
  local output=""
  if ! output="$("$VERIFY_SSH_BIN" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -i "$key_path" \
    "ae@${guest_ip}" \
    "sudo -n bash -lc '/usr/local/bin/k1s-gpu-contract-check'" 2>&1)"; then
    if [[ -n "$output" ]]; then
      printf '%s\n' "$output" >&2
    fi
    return 1
  fi
  return 0
}

stamp_gpu_metadata() {
  local meta_file="$1"
  local tmp_meta=""
  tmp_meta="$(mktemp)"
  jq '
    . + {
      gpu_bootstrap_ready:true,
      gpu_runtime_handler:"nvidia",
      gpu_seed_images_ready:true
    }
  ' "$meta_file" >"$tmp_meta"
  mv "$tmp_meta" "$meta_file"
}

write_verify_variant() {
  local variant_file="$1"
  local image="$2"
  local image_variant="$3"
  local bridge="$4"
  local cidr="$5"
  local gateway="$6"
  local guest_ip="$7"
  local test_id="$8"
  local gpu="$9"
  local disk_gb="${10}"

  cat >"$variant_file" <<YAML
name: image-verify-${image_variant}
test_id: ${test_id}
network:
  bridge: ${bridge}
  cidr: ${cidr}
  gateway: ${gateway}
vm:
  memory_mb: 4096
  vcpus: 2
  disk_gb: ${disk_gb}
images:
  base: ${image}
  gpu: ${image}
hosts:
  - name: image-verify-${image_variant}
    ip: ${guest_ip}
    role: k1s-core
    gpu: ${gpu}
YAML
}

boot_verify_image() (
  set -euo pipefail

  local image_variant="$1"
  local image="$2"
  local bridge="" cidr="" gateway="" guest_ip="" gpu="" test_id=""
  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  local suffix="$(( $$ % 10000 ))"
  local tmp_dir run_id variant_file state_dir run_dir required_disk_gb=""
  local verify_failed=0

  case "$image_variant" in
    base)
      bridge="k1svb${suffix}"
      cidr="192.168.251.0/24"
      gateway="192.168.251.1"
      guest_ip="192.168.251.10"
      gpu="false"
      test_id="901"
      ;;
    gpu)
      bridge="k1svg${suffix}"
      cidr="192.168.252.0/24"
      gateway="192.168.252.1"
      guest_ip="192.168.252.10"
      gpu="true"
      test_id="902"
      ;;
    *)
      echo "[image-verify] unsupported boot verify variant: ${image_variant}" >&2
      exit 2
      ;;
  esac

  tmp_dir="$(mktemp -d)"
  run_id="image-verify-${image_variant}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  variant_file="$tmp_dir/variant.yaml"
  state_dir="$ROOT_DIR/state/lab-vm/$run_id"
  run_dir="$ROOT_DIR/runs/$run_id"
  required_disk_gb="$(image_virtual_size_gib "$image")"

  summarize_failure() {
    local console_log="" inspect_report=""
    local -a console_logs=()

    if [[ ! -d "$state_dir" ]]; then
      echo "[image-verify] preserved failed verifier state unavailable: $state_dir" >&2
      return 0
    fi

    echo "[image-verify] preserved failed verifier state: $state_dir" >&2
    if [[ -d "$run_dir" ]]; then
      echo "[image-verify] preserved failed verifier run: $run_dir" >&2
    fi

    if [[ -d "$state_dir/logs" ]]; then
      mapfile -t console_logs < <(find "$state_dir/logs" -maxdepth 1 -type f -name '*.console.log' | sort)
      if [[ "${#console_logs[@]}" -gt 0 ]]; then
        console_log="${console_logs[0]}"
        echo "[image-verify] console log tail: $console_log" >&2
        tail -n 80 "$console_log" >&2 || true
        if grep -Eq 'Gave up waiting for root file system device|Dropping to a shell|UUID=.*does not exist' "$console_log"; then
          echo "[image-verify] boot failed before ssh; root filesystem did not mount" >&2
        fi
      fi
    fi

    if [[ -f "$state_dir/image-verify-${image_variant}.qcow2" ]]; then
      inspect_report="$state_dir/boot-contract.txt"
      if bash "$INSPECT_QCOW_BOOT_SCRIPT" "$state_dir/image-verify-${image_variant}.qcow2" >"$inspect_report" 2>&1; then
        echo "[image-verify] boot-contract report: $inspect_report" >&2
      else
        echo "[image-verify] boot-contract inspection failed; see $inspect_report" >&2
      fi
    fi
  }

  cleanup() {
    local -a down_args=()
    down_args=(
      --variant "$variant_file"
      --run-id "$run_id"
      --destroy-network
      --best-effort
    )
    if [[ "$verify_failed" -eq 0 || "$PURGE_FAILED" -eq 1 ]]; then
      down_args+=(--purge)
    fi
    bash "$VARIANT_DOWN_SCRIPT" \
      "${down_args[@]}" >/dev/null 2>&1 || true
    if [[ "$verify_failed" -eq 1 && "$PURGE_FAILED" -eq 0 ]]; then
      summarize_failure
    else
      rm -rf "$run_dir"
    fi
    rm -rf "$tmp_dir"
  }
  trap cleanup EXIT

  write_verify_variant "$variant_file" "$image" "$image_variant" "$bridge" "$cidr" "$gateway" "$guest_ip" "$test_id" "$gpu" "$required_disk_gb"

  echo "[image-verify] ssh key path: $key_path"
  echo "[image-verify] validating boot contract: $image"
  bash "$ASSERT_IMAGE_BOOT_CONTRACT_SCRIPT" "$image"

  echo "[image-verify] boot smoke start: $image"
  if ! bash "$VARIANT_UP_SCRIPT" --variant "$variant_file" --run-id "$run_id"; then
    verify_failed=1
    return 1
  fi

  if ! "$VERIFY_SSH_BIN" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -i "$key_path" \
    "ae@${guest_ip}" \
    "sudo bash -lc 'source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh && ensure_vm_bootstrap_prereqs'"; then
    verify_failed=1
    return 1
  fi

  if [[ "$image_variant" == "gpu" ]]; then
    if ! verify_gpu_guest_contract "$guest_ip"; then
      verify_failed=1
      return 1
    fi
  fi

  echo "[image-verify] boot smoke ok: $image"
)

check_one() {
  local variant="$1"
  local image="$IMAGE_DIR/ubuntu-22.04-k1s-${variant}.qcow2"
  local sha_file="${image}.sha256"
  local meta_file="${image}.meta.json"
  local image_dir_abs=""

  [[ -f "$image" ]] || { echo "missing image: $image" >&2; exit 1; }
  [[ -f "$sha_file" ]] || { echo "missing checksum: $sha_file" >&2; exit 1; }
  [[ -f "$meta_file" ]] || { echo "missing metadata: $meta_file" >&2; exit 1; }

  image_dir_abs="$(cd "$(dirname "$image")" && pwd -P)"
  image="${image_dir_abs}/$(basename "$image")"
  sha_file="${image}.sha256"
  meta_file="${image}.meta.json"

  (cd "$image_dir_abs" && sha256sum -c "$(basename "$sha_file")")
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

  if [[ "$METADATA_ONLY" -eq 0 ]]; then
    boot_verify_image "$variant" "$image"
    if [[ "$variant" == "gpu" ]]; then
      stamp_gpu_metadata "$meta_file"
    fi
  fi

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
