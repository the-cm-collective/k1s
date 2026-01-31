#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/cri_image_prewarm.sh [options]

Options:
  --endpoint <endpoint>     CRI endpoint (defaults to AE_CRI_ENDPOINT)
  --image <ref>             Image to pull (repeatable)
  --images-file <path>      Newline-delimited list of images to pull
  -h, --help                Show this help

Environment:
  AE_CRI_ENDPOINT, CRICTL_BIN, AE_CRI_PREWARM_IMAGES, CRICTL_PULL_FLAGS

Notes:
  - AE_CRI_PREWARM_IMAGES can be a space-delimited list of image refs.
  - CRICTL_PULL_FLAGS is passed verbatim to crictl pull (if your crictl supports it).

Examples:
  AE_CRI_PREWARM_IMAGES="mendhak/http-https-echo:37" scripts/cri_image_prewarm.sh
  scripts/cri_image_prewarm.sh --image registry.k1s.home.arpa:32000/demo-green:latest
USAGE
}

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
images=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)
      endpoint="${2:?missing endpoint}"; shift ;;
    --image)
      images+=("${2:?missing image}"); shift ;;
    --images-file)
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        images+=("$line")
      done < "${2:?missing file}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage; exit 2 ;;
  esac
  shift
 done

if [[ ${#images[@]} -eq 0 && -n "${AE_CRI_PREWARM_IMAGES:-}" ]]; then
  read -r -a images <<< "${AE_CRI_PREWARM_IMAGES}"
fi

if [[ ${#images[@]} -eq 0 ]]; then
  echo "No images specified (use --image or AE_CRI_PREWARM_IMAGES)" >&2
  exit 1
fi

crictl_bin="${CRICTL_BIN:-crictl}"
if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found (set CRICTL_BIN or install crictl)" >&2
  exit 1
fi

pull_flags=()
if [[ -n "${CRICTL_PULL_FLAGS:-}" ]]; then
  read -r -a pull_flags <<< "${CRICTL_PULL_FLAGS}"
fi

for img in "${images[@]}"; do
  echo "CRI prewarm: ${img}"
  "$crictl_bin" --runtime-endpoint "$endpoint" pull "${pull_flags[@]}" "$img"
done
