#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/build_cri_apishim_image.sh [options]

Build apishim image locally, push to registry, and optionally CRI pull for verification.

Options:
  --image <ref>              Target image reference (default: AE_APISHIM_IMAGE or localhost/k1s-apishim:dev)
  --registry <host:port>     Registry host override (default: AE_CRI_REGISTRY or AE_REGISTRY_HOST)
  --tag <name:tag>           Tag/path when --image is omitted (default: k1s-apishim:dev)
  --engine <name>            Build/push backend (nerdctl|podman|docker)
  --push                     Push target image after build (default: enabled)
  --no-push                  Disable push
  --pull-cri                 Pull target image via CRI after push/build (default: enabled)
  --no-pull-cri              Disable CRI pull verification
  --cri-endpoint <uri>       CRI endpoint (default: AE_CRI_ENDPOINT)
  -h, --help                 Show this help

Environment:
  AE_CRI_IMAGE_BUILD_BACKEND  Preferred build backend (nerdctl|podman|docker)
  AE_CRI_LOCAL_BUILD_BACKEND  Legacy shared backend override (nerdctl|podman|docker)
USAGE
}

image="${AE_APISHIM_IMAGE:-}"
registry="${AE_CRI_REGISTRY:-${AE_REGISTRY_HOST:-}}"
tag="k1s-apishim:dev"
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
echo "[build-cri-apishim] backend=${engine} target=${image}"

if [[ ! -f "${root_dir}/ops/images/apishim.Dockerfile" ]]; then
  echo "Missing Dockerfile: ${root_dir}/ops/images/apishim.Dockerfile" >&2
  exit 1
fi

"$engine" build -f "${root_dir}/ops/images/apishim.Dockerfile" -t "$image" "$root_dir"

if (( push == 1 )); then
  echo "[build-cri-apishim] pushing ${image}"
  engine_push "$image"
fi

if (( pull_cri == 1 )); then
  if ! command -v crictl >/dev/null 2>&1; then
    echo "crictl not found; cannot perform CRI pull verification" >&2
    exit 1
  fi
  echo "[build-cri-apishim] CRI pull verify ${image}"
  crictl --runtime-endpoint "$cri_endpoint" pull "$image" >/dev/null
fi

echo "[build-cri-apishim] ready ${image}"
