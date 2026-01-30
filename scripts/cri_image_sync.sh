#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/cri_image_sync.sh [options]

Options:
  --registry <host:port>   Registry host to push/pull (defaults to AE_REGISTRY_HOST)
  --endpoint <endpoint>    CRI endpoint (defaults to AE_CRI_ENDPOINT)
  --engine <docker|podman> Push engine override (defaults: podman then docker)
  --image <ref>            Image to pull via CRI (may be repeated)
  --local-image <ref>      Local image to tag/push to registry, then pull via CRI
  --images-file <path>     Newline-delimited list of images to pull
  --local-images-file <path> Newline-delimited list of local images to push
  -h, --help               Show this help

Environment:
  AE_REGISTRY_HOST, AE_CRI_ENDPOINT, AE_REGISTRY_PUSH_BIN, CRICTL_BIN

Examples:
  scripts/cri_image_sync.sh --registry localhost:5001 \
    --local-image demo-green:latest --image mendhak/http-https-echo:37
USAGE
}

registry_ref() {
  local registry="$1"
  local image="$2"
  local name="$image"
  local digest=""
  local tag=""

  if [[ "$name" == *@* ]]; then
    digest="@${name#*@}"
    name="${name%@*}"
  fi
  if [[ "${name##*/}" == *:* ]]; then
    tag=":${name##*:}"
    name="${name%:*}"
  fi

  local first="${name%%/*}"
  local rest="${name#*/}"
  if [[ "$name" != "$rest" ]]; then
    if [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]; then
      name="$rest"
    fi
  fi

  echo "${registry}/${name}${tag}${digest}"
}

registry="${AE_REGISTRY_HOST:-}"
endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
engine="${AE_REGISTRY_PUSH_BIN:-}"
images=()
local_images=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)
      registry="${2:?missing registry}"; shift ;;
    --endpoint)
      endpoint="${2:?missing endpoint}"; shift ;;
    --engine)
      engine="${2:?missing engine}"; shift ;;
    --image)
      images+=("${2:?missing image}"); shift ;;
    --local-image)
      local_images+=("${2:?missing local image}"); shift ;;
    --images-file)
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        images+=("$line")
      done < "${2:?missing file}"; shift ;;
    --local-images-file)
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local_images+=("$line")
      done < "${2:?missing file}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage; exit 2 ;;
  esac
  shift
 done

if [[ -z "$registry" ]]; then
  echo "AE_REGISTRY_HOST/--registry is required" >&2
  exit 1
fi

if [[ -z "$engine" ]]; then
  if command -v podman >/dev/null 2>&1; then
    engine="podman"
  elif command -v docker >/dev/null 2>&1; then
    engine="docker"
  else
    echo "No container engine found (podman or docker required)" >&2
    exit 1
  fi
fi

crictl_bin="${CRICTL_BIN:-crictl}"
if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found (set CRICTL_BIN or install crictl)" >&2
  exit 1
fi

if [[ ${#local_images[@]} -gt 0 ]]; then
  echo "Pushing local images to ${registry} using ${engine}"
fi
for img in "${local_images[@]}"; do
  target=$(registry_ref "$registry" "$img")
  "$engine" tag "$img" "$target"
  "$engine" push "$target"
  images+=("$target")
done

if [[ ${#images[@]} -eq 0 ]]; then
  echo "No images specified; nothing to do." >&2
  exit 0
fi

for img in "${images[@]}"; do
  echo "CRI pull: ${img}"
  "$crictl_bin" --runtime-endpoint "$endpoint" pull "$img"
done
