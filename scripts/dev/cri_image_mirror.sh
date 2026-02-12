#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/dev/cri_image_mirror.sh [options]

Mirror an image into the configured OCI registry and optionally verify CRI pull.

Options:
  --source <ref>            Source image reference to pull
  --target <ref>            Target image reference to push
  --engine <name>           Local engine backend (nerdctl|podman|docker)
  --pull-cri                Pull target image via CRI after push (default: enabled)
  --no-pull-cri             Disable CRI pull verification
  --cri-endpoint <uri>      CRI endpoint (default: AE_CRI_ENDPOINT)
  -h, --help                Show this help

Environment:
  AE_CRI_LOCAL_BUILD_BACKEND  Preferred backend (nerdctl|podman|docker)
USAGE
}

source_image=""
target_image=""
engine=""
pull_cri=1
cri_endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      source_image="${2:?missing source image}"; shift ;;
    --target)
      target_image="${2:?missing target image}"; shift ;;
    --engine)
      engine="${2:?missing engine name}"; shift ;;
    --pull-cri)
      pull_cri=1 ;;
    --no-pull-cri)
      pull_cri=0 ;;
    --cri-endpoint)
      cri_endpoint="${2:?missing cri endpoint}"; shift ;;
    -h|--help)
      usage
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2 ;;
  esac
  shift
done

if [[ -z "$source_image" || -z "$target_image" ]]; then
  echo "Both --source and --target are required." >&2
  usage
  exit 2
fi

has_registry_prefix() {
  local ref="$1"
  [[ "$ref" == */* ]] || return 1
  local first="${ref%%/*}"
  [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]
}

resolve_engine() {
  local prefer="${engine:-${AE_CRI_LOCAL_BUILD_BACKEND:-}}"
  if [[ -n "$prefer" ]]; then
    if ! command -v "$prefer" >/dev/null 2>&1; then
      echo "Requested backend '$prefer' not found" >&2
      exit 1
    fi
    engine="$prefer"
    return 0
  fi
  local candidate
  for candidate in nerdctl podman docker; do
    if command -v "$candidate" >/dev/null 2>&1; then
      engine="$candidate"
      return 0
    fi
  done
  echo "No supported backend found (nerdctl/podman/docker)" >&2
  exit 1
}

engine_pull() {
  local image="$1"
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    case "$engine" in
      nerdctl) "$engine" --insecure-registry pull "$image"; return ;;
      podman) "$engine" pull --tls-verify=false "$image"; return ;;
      docker) ;;
    esac
  fi
  "$engine" pull "$image"
}

engine_push() {
  local image="$1"
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    case "$engine" in
      nerdctl) "$engine" --insecure-registry push "$image"; return ;;
      podman) "$engine" push --tls-verify=false "$image"; return ;;
      docker) ;;
    esac
  fi
  "$engine" push "$image"
}

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

if ! has_registry_prefix "$target_image"; then
  echo "Target image '$target_image' is not registry-qualified." >&2
  exit 1
fi
if [[ "$target_image" == localhost/* && "$target_image" != localhost:*/* ]]; then
  echo "Target image '$target_image' is invalid for push/pull; use localhost:<port>/..." >&2
  exit 1
fi

resolve_engine
echo "[cri-image-mirror] backend=${engine} source=${source_image} target=${target_image}"

engine_pull "$source_image"
if [[ "$source_image" != "$target_image" ]]; then
  "$engine" tag "$source_image" "$target_image"
fi
echo "[cri-image-mirror] pushing ${target_image}"
engine_push "$target_image"

if (( pull_cri == 1 )); then
  if ! command -v crictl >/dev/null 2>&1; then
    echo "crictl not found; cannot perform CRI pull verification" >&2
    exit 1
  fi
  echo "[cri-image-mirror] CRI pull verify ${target_image}"
  crictl --runtime-endpoint "$cri_endpoint" pull "$target_image" >/dev/null
fi

echo "[cri-image-mirror] ready ${target_image}"
