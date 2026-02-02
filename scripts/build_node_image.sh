#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/build_node_image.sh [options]

Options:
  --registry <host:port>  Registry host (defaults to AE_REGISTRY_HOST)
  --tag <name:tag>        Image name/tag (default: k1s-node:latest)
  --push                  Push after build
  --engine <docker|podman> Build engine (defaults: podman then docker)
  -h, --help              Show this help

Environment (optional):
  CRICTL_VERSION

Examples:
  AE_REGISTRY_HOST=registry.k1s.home.arpa:32000 \
    scripts/build_node_image.sh --push
USAGE
}

registry="${AE_REGISTRY_HOST:-}"
tag="k1s-node:latest"
push=0
engine=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)
      registry="${2:?missing registry}"; shift ;;
    --tag)
      tag="${2:?missing tag}"; shift ;;
    --push)
      push=1 ;;
    --engine)
      engine="${2:?missing engine}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage; exit 2 ;;
  esac
  shift
done

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

image="$tag"
if [[ -n "$registry" ]]; then
  image="${registry}/${tag}"
fi

build_args=()
if [[ -n "${CRICTL_VERSION:-}" ]]; then
  build_args+=(--build-arg "CRICTL_VERSION=${CRICTL_VERSION}")
fi

$engine build -f ops/images/node.Dockerfile -t "$image" "${build_args[@]}" .

if [[ $push -eq 1 ]]; then
  $engine push "$image"
fi

echo "Built ${image}"
