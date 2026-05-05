#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
self="${root_dir}/scripts/dev/run_host_a_vllm_validate_capture.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/run_host_a_vllm_validate_capture.sh [options]

Rebuild the Host A custom vLLM image, run the Host A validation flow, and
capture the full terminal transcript under runs/transcripts/.

Options:
  --run-id <id>              Override the generated run id
  --transcript-dir <path>    Transcript output directory (default: runs/transcripts)
  --image <ref>              Test image reference (default: docker.io/library/k1s-vllm-openai:host-a-cu121-v2)
  --controller-env <path>    Controller env file (default: state/profiles/k1s-core/controller.env)
  --vm-name <name>           Host A VM name (default: k1s-core-a-gpu)
  --cell-lane <name>         Validation lane (default: cell-a-single)
  --build-engine <name>      Build backend (default: podman)
  --seed-engine <name>       Seed bundle backend (default: podman)
  --inner                    Internal flag used by transcript capture
  -h, --help                 Show this help
USAGE
}

run_id="host-a-$(date -u +%Y%m%dT%H%M%SZ)"
transcript_dir="runs/transcripts"
image="docker.io/library/k1s-vllm-openai:host-a-cu121-v2"
controller_env="state/profiles/k1s-core/controller.env"
vm_name="k1s-core-a-gpu"
cell_lane="cell-a-single"
build_engine="${AE_CRI_IMAGE_BUILD_BACKEND:-podman}"
seed_engine="${AE_CRI_CACHE_SEED_ENGINE:-podman}"
inner=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      run_id="${2:?missing run id}"; shift ;;
    --transcript-dir)
      transcript_dir="${2:?missing transcript dir}"; shift ;;
    --image)
      image="${2:?missing image ref}"; shift ;;
    --controller-env)
      controller_env="${2:?missing controller env path}"; shift ;;
    --vm-name)
      vm_name="${2:?missing vm name}"; shift ;;
    --cell-lane)
      cell_lane="${2:?missing cell lane}"; shift ;;
    --build-engine)
      build_engine="${2:?missing build engine}"; shift ;;
    --seed-engine)
      seed_engine="${2:?missing seed engine}"; shift ;;
    --inner)
      inner=1 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2 ;;
  esac
  shift
done

cd "$root_dir"

if (( inner == 1 )); then
  export AE_CRI_IMAGE_BUILD_BACKEND="$build_engine"
  export AE_CRI_CACHE_SEED_ENGINE="$seed_engine"

  python scripts/dev/host_a_netfs_lane.py resume --restart-controller
  curl -fsS http://127.0.0.1:9110/healthz
  curl -fsS -H 'X-Agent-Token: devtoken' \
    'http://127.0.0.1:9110/v1/nodes/core-a--hub/overlay' | python -m json.tool

  bash scripts/build_cri_host_a_vllm_image.sh \
    --engine "$build_engine" \
    --image "$image" \
    --no-push \
    --no-pull-cri

  python scripts/dev/f0n_nvidia_validate.py collect \
    --run-id "$run_id" \
    --vm-name "$vm_name" \
    --controller-env "$controller_env" \
    --cell-lane "$cell_lane" \
    --test-vllm-image "$image"
  exit 0
fi

mkdir -p "$transcript_dir"
transcript="${transcript_dir%/}/${run_id}.terminal.log"

cmd=(
  bash "$self"
  --inner
  --run-id "$run_id"
  --transcript-dir "$transcript_dir"
  --image "$image"
  --controller-env "$controller_env"
  --vm-name "$vm_name"
  --cell-lane "$cell_lane"
  --build-engine "$build_engine"
  --seed-engine "$seed_engine"
)

printf -v script_cmd '%q ' "${cmd[@]}"

rc=0
if script -qefc "$script_cmd" "$transcript"; then
  rc=0
else
  rc=$?
fi

printf 'run_id: %s\n' "$run_id"
printf 'transcript: %s\n' "$transcript"
printf 'artifacts: %s\n' "runs/$run_id"

exit "$rc"
