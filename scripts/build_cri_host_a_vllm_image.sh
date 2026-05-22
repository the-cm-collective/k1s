#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/build_cri_host_a_vllm_image.sh [options]

Build the Host A custom vLLM image locally, push to registry, and optionally CRI pull for verification.

Options:
  --image <ref>              Target image reference (default: AE_HOST_A_VLLM_IMAGE or docker.io/library/k1s-vllm-openai:host-a-cu121-v2)
  --registry <host:port>     Registry host override (default: AE_CRI_REGISTRY or AE_REGISTRY_HOST)
  --tag <name:tag>           Tag/path when --image is omitted (default: k1s-vllm-openai:host-a-cu121-v2)
  --engine <name>            Build/push backend (nerdctl|podman|docker)
  --push                     Push target image after build (default: enabled)
  --no-push                  Disable push
  --pull-cri                 Pull target image via CRI after push/build (default: enabled)
  --no-pull-cri              Disable CRI pull verification
  --cri-endpoint <uri>       CRI endpoint (default: AE_CRI_ENDPOINT)
  -h, --help                 Show this help

Environment:
  AE_CRI_IMAGE_BUILD_BACKEND     Preferred build backend (nerdctl|podman|docker)
  AE_CRI_LOCAL_BUILD_BACKEND     Legacy shared backend override (nerdctl|podman|docker)
  AE_HOST_A_VLLM_BASE_IMAGE      Dockerfile base image (default: nvidia/cuda:12.1.0-devel-ubuntu22.04)
  AE_HOST_A_VLLM_TORCH_VERSION   PyTorch version (default: 2.4.0)
  AE_HOST_A_VLLM_TORCHVISION_VERSION torchvision version (default: 0.19.0)
  AE_HOST_A_VLLM_TORCHAUDIO_VERSION torchaudio version (default: 2.4.0)
  AE_HOST_A_VLLM_VERSION         vLLM package version (default: 0.6.2)
  AE_HOST_A_TRANSFORMERS_VERSION transformers package version (default: 4.45.0)
  AE_HOST_A_VLLM_GIT_REF         Legacy alias; leading "v" is stripped when deriving the package version
USAGE
}

image="${AE_HOST_A_VLLM_IMAGE:-}"
registry="${AE_CRI_REGISTRY:-${AE_REGISTRY_HOST:-}}"
tag="docker.io/library/k1s-vllm-openai:host-a-cu121-v2"
engine=""
push=1
pull_cri=1
cri_endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      image="${2:?missing image ref}"; shift ;;
    --registry)
      registry="${2:?missing registry host}"; shift ;;
    --tag)
      tag="${2:?missing tag}"; shift ;;
    --engine)
      engine="${2:?missing engine}"; shift ;;
    --push)
      push=1 ;;
    --no-push)
      push=0 ;;
    --pull-cri)
      pull_cri=1 ;;
    --no-pull-cri)
      pull_cri=0 ;;
    --cri-endpoint)
      cri_endpoint="${2:?missing cri endpoint}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2 ;;
  esac
  shift
done

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

has_registry_prefix() {
  local ref="$1"
  [[ "$ref" == */* ]] || return 1
  local first="${ref%%/*}"
  [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]
}

registry_ref() {
  local reg="$1"
  local src="$2"
  local name="$src"
  local digest=""
  local suffix=""
  if [[ "$name" == *@* ]]; then
    digest="@${name#*@}"
    name="${name%@*}"
  fi
  local last="${name##*/}"
  if [[ "$last" == *:* ]]; then
    suffix=":${last##*:}"
    name="${name%:*}"
  fi
  if has_registry_prefix "$name"; then
    name="${name#*/}"
  fi
  echo "${reg%/}/${name#/}${suffix}${digest}"
}

resolve_engine() {
  local prefer="${engine:-${AE_CRI_IMAGE_BUILD_BACKEND:-${AE_CRI_LOCAL_BUILD_BACKEND:-}}}"
  if [[ -n "$prefer" ]]; then
    if [[ "$prefer" == "ctr" ]]; then
      echo "Requested build backend 'ctr' is invalid; ctr cannot build local images." >&2
      echo "Use AE_CRI_IMAGE_BUILD_BACKEND=podman|docker|nerdctl (or AE_CRI_LOCAL_BUILD_BACKEND)." >&2
      exit 1
    fi
    if ! command -v "$prefer" >/dev/null 2>&1; then
      echo "Requested build backend '$prefer' not found" >&2
      exit 1
    fi
    if [[ "$prefer" == "nerdctl" ]] && ! command -v buildctl >/dev/null 2>&1; then
      echo "Requested build backend 'nerdctl' requires buildctl, but buildctl is not installed." >&2
      echo "Use AE_CRI_LOCAL_BUILD_BACKEND=podman (or docker), or install buildctl." >&2
      exit 1
    fi
    engine="$prefer"
    return 0
  fi
  local candidate
  for candidate in nerdctl podman docker; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if [[ "$candidate" == "nerdctl" ]] && ! command -v buildctl >/dev/null 2>&1; then
        continue
      fi
      engine="$candidate"
      return 0
    fi
  done
  if command -v nerdctl >/dev/null 2>&1 && ! command -v buildctl >/dev/null 2>&1; then
    echo "nerdctl is installed but buildctl is missing; nerdctl build cannot run." >&2
  fi
  echo "No supported local build backend found (nerdctl+buildctl/podman/docker)" >&2
  exit 1
}

engine_push() {
  local target="$1"
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    case "$engine" in
      nerdctl) "$engine" --insecure-registry push "$target"; return ;;
      podman) "$engine" push --tls-verify=false "$target"; return ;;
      docker) ;;
    esac
  fi
  "$engine" push "$target"
}

engine_build() {
  local target="$1"
  local dockerfile="${root_dir}/ops/images/host-a-vllm-openai.Dockerfile"
  local vllm_version="${AE_HOST_A_VLLM_VERSION:-${AE_HOST_A_VLLM_GIT_REF:-0.6.2}}"
  local transformers_version="${AE_HOST_A_TRANSFORMERS_VERSION:-4.45.0}"
  vllm_version="${vllm_version#v}"
  local -a build_args=(
    --build-arg "BASE_IMAGE=${AE_HOST_A_VLLM_BASE_IMAGE:-nvidia/cuda:12.1.0-devel-ubuntu22.04}"
    --build-arg "TORCH_VERSION=${AE_HOST_A_VLLM_TORCH_VERSION:-2.4.0}"
    --build-arg "TORCHVISION_VERSION=${AE_HOST_A_VLLM_TORCHVISION_VERSION:-0.19.0}"
    --build-arg "TORCHAUDIO_VERSION=${AE_HOST_A_VLLM_TORCHAUDIO_VERSION:-2.4.0}"
    --build-arg "VLLM_VERSION=${vllm_version}"
    --build-arg "TRANSFORMERS_VERSION=${transformers_version}"
  )
  case "$engine" in
    podman|docker)
      echo "[build-host-a-vllm] using host networking for ${engine} build"
      "$engine" build --network host -f "$dockerfile" -t "$target" "${build_args[@]}" "$root_dir"
      ;;
    *)
      "$engine" build -f "$dockerfile" -t "$target" "${build_args[@]}" "$root_dir"
      ;;
  esac
}

if [[ -z "$image" ]]; then
  image="$tag"
fi

if [[ "$image" == localhost/* && "$image" != localhost:*/* ]]; then
  if [[ -z "$registry" ]]; then
    echo "Image '${image}' requires a registry host. Set AE_CRI_REGISTRY or pass --registry." >&2
    exit 1
  fi
  image="$(registry_ref "$registry" "$image")"
fi
if ! has_registry_prefix "$image"; then
  if [[ -z "$registry" ]]; then
    echo "Image '${image}' is not registry-qualified. Set AE_CRI_REGISTRY or pass --registry." >&2
    exit 1
  fi
  image="$(registry_ref "$registry" "$image")"
fi
if [[ "$image" == localhost/* && "$image" != localhost:*/* ]]; then
  echo "Registry reference '${image}' is invalid for push/pull. Use localhost:<port>/... or a real host." >&2
  exit 1
fi

resolve_engine
echo "[build-host-a-vllm] backend=${engine} target=${image}"

if [[ ! -f "${root_dir}/ops/images/host-a-vllm-openai.Dockerfile" ]]; then
  echo "Missing Dockerfile: ${root_dir}/ops/images/host-a-vllm-openai.Dockerfile" >&2
  exit 1
fi

engine_build "$image"

if (( push == 1 )); then
  echo "[build-host-a-vllm] pushing ${image}"
  engine_push "$image"
fi

if (( pull_cri == 1 )); then
  if ! command -v crictl >/dev/null 2>&1; then
    echo "crictl not found; cannot perform CRI pull verification" >&2
    exit 1
  fi
  echo "[build-host-a-vllm] CRI pull verify ${image}"
  crictl --runtime-endpoint "$cri_endpoint" pull "$image" >/dev/null
fi

echo "[build-host-a-vllm] ready ${image}"
