#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/dev/cri_image_mirror.sh [options]

Mirror an image into the configured OCI registry and optionally verify CRI pull.

Options:
  --source <ref>            Source image reference to pull
  --target <ref>            Target image reference to push
  --engine <name>           Local engine backend (nerdctl|podman|docker|ctr)
  --pull-cri                Pull target image via CRI after push (default: enabled)
  --no-pull-cri             Disable CRI pull verification
  --cri-endpoint <uri>      CRI endpoint (default: AE_CRI_ENDPOINT)
  -h, --help                Show this help

Environment:
  AE_CRI_IMAGE_MIRROR_BACKEND Preferred mirror backend (nerdctl|podman|docker|ctr)
  AE_CRI_LOCAL_BUILD_BACKEND  Legacy shared backend override (nerdctl|podman|docker|ctr)
  AE_CRI_IMAGE_MIRROR_ALWAYS_PULL  Set truthy to force remote pull even when source exists locally
  AE_CTR_NAMESPACE            ctr namespace (default: k8s.io)
  AE_CTR_HOSTS_DIR            ctr hosts dir (default: /etc/containerd/certs.d)
  AE_CTR_PLATFORM             ctr platform override (default: host arch, e.g. linux/amd64)
USAGE
}

source_image=""
target_image=""
engine=""
pull_cri=1
cri_endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
ctr_namespace="${AE_CTR_NAMESPACE:-k8s.io}"
ctr_hosts_dir="${AE_CTR_HOSTS_DIR:-/etc/containerd/certs.d}"
ctr_platform="${AE_CTR_PLATFORM:-}"
ctr_source_pulled=0

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
  local prefer="${engine:-${AE_CRI_IMAGE_MIRROR_BACKEND:-${AE_CRI_LOCAL_BUILD_BACKEND:-}}}"
  if [[ -n "$prefer" ]]; then
    if ! command -v "$prefer" >/dev/null 2>&1; then
      echo "Requested backend '$prefer' not found" >&2
      exit 1
    fi
    engine="$prefer"
    return 0
  fi
  local candidate
  for candidate in nerdctl podman docker ctr; do
    if command -v "$candidate" >/dev/null 2>&1; then
      engine="$candidate"
      return 0
    fi
  done
  echo "No supported backend found (nerdctl/podman/docker/ctr)" >&2
  exit 1
}

engine_pull() {
  local image="$1"
  if [[ "$engine" == "ctr" ]]; then
    local force_pull=0
    if is_truthy "${AE_CRI_IMAGE_MIRROR_ALWAYS_PULL:-0}"; then
      force_pull=1
    elif engine_has_image "$image"; then
      echo "[cri-image-mirror] source already cached: ${image}"
    else
      force_pull=1
    fi
    if (( force_pull == 0 )); then
      ctr_source_pulled=0
      return
    fi
    local -a cmd=(ctr -n "$ctr_namespace" images pull)
    if [[ -n "$ctr_platform" ]]; then
      cmd+=(--platform "$ctr_platform")
    fi
    if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
      cmd+=(--plain-http)
    fi
    if [[ -d "$ctr_hosts_dir" ]]; then
      cmd+=(--hosts-dir "$ctr_hosts_dir")
    fi
    cmd+=("$image")
    "${cmd[@]}"
    ctr_source_pulled=1
    return
  fi
  if ! is_truthy "${AE_CRI_IMAGE_MIRROR_ALWAYS_PULL:-0}" && engine_has_image "$image"; then
    echo "[cri-image-mirror] source already cached: ${image}"
    return
  fi
  if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
    case "$engine" in
      nerdctl) "$engine" --insecure-registry pull "$image"; return ;;
      podman) "$engine" pull --tls-verify=false "$image"; return ;;
      docker) ;;
    esac
  fi
  "$engine" pull "$image"
}

engine_has_image() {
  local image="$1"
  case "$engine" in
    ctr)
      ctr -n "$ctr_namespace" images ls -q 2>/dev/null | grep -Fx -- "$image" >/dev/null 2>&1
      ;;
    nerdctl)
      nerdctl image inspect "$image" >/dev/null 2>&1
      ;;
    podman)
      podman image inspect "$image" >/dev/null 2>&1
      ;;
    docker)
      docker image inspect "$image" >/dev/null 2>&1
      ;;
    *)
      return 1
      ;;
  esac
}

engine_push() {
  local image="$1"
  local local_ref="${2:-}"
  if [[ "$engine" == "ctr" ]]; then
    local -a cmd=(ctr -n "$ctr_namespace" images push)
    if [[ -n "$ctr_platform" ]]; then
      cmd+=(--platform "$ctr_platform")
    fi
    if is_truthy "${AE_CRI_REGISTRY_INSECURE:-0}"; then
      cmd+=(--plain-http)
    fi
    if [[ -d "$ctr_hosts_dir" ]]; then
      cmd+=(--hosts-dir "$ctr_hosts_dir")
    fi
    cmd+=("$image")
    if [[ -n "$local_ref" ]]; then
      cmd+=("$local_ref")
    fi
    "${cmd[@]}"
    return
  fi
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

default_ctr_platform() {
  case "$(uname -m 2>/dev/null || true)" in
    x86_64|amd64) printf '%s' "linux/amd64" ;;
    aarch64|arm64) printf '%s' "linux/arm64" ;;
    armv7l|armv7) printf '%s' "linux/arm/v7" ;;
    *)
      # Fallback keeps behavior deterministic on uncommon architectures.
      printf '%s' "linux/amd64"
      ;;
  esac
}

engine_tag() {
  local source="$1"
  local target="$2"
  if [[ "$engine" == "ctr" ]]; then
    ctr -n "$ctr_namespace" images tag --force "$source" "$target"
    return
  fi
  "$engine" tag "$source" "$target"
}

ctr_supports_convert() {
  ctr images convert --help >/dev/null 2>&1
}

ctr_delete_target_ref() {
  local reason="${1:-refresh}"
  echo "[cri-image-mirror] evicting ctr image ref (${reason}): ${target_image}"
  ctr -n "$ctr_namespace" images delete "$target_image" >/dev/null 2>&1 || true
}

ctr_refresh_cri_target() {
  local reason="${1:-refresh}"
  if command -v crictl >/dev/null 2>&1; then
    echo "[cri-image-mirror] evicting CRI image cache (${reason}): ${target_image}"
    crictl --runtime-endpoint "$cri_endpoint" rmi "$target_image" >/dev/null 2>&1 || true
  fi
  ctr_delete_target_ref "$reason"
}

ctr_prepare_push_ref() {
  local source="$1"
  if ! ctr_supports_convert; then
    echo "ctr backend requires 'ctr images convert' support; set AE_CRI_IMAGE_MIRROR_BACKEND=podman|nerdctl|docker (or AE_CRI_LOCAL_BUILD_BACKEND)" >&2
    exit 1
  fi
  ctr_delete_target_ref "pre-convert"
  echo "[cri-image-mirror] normalizing ctr source to ${ctr_platform}: ${target_image}"
  if ctr -n "$ctr_namespace" images convert --platform "$ctr_platform" "$source" "$target_image"; then
    return
  fi
  if (( ctr_source_pulled == 0 )); then
    echo "[cri-image-mirror] refreshing ctr source content for ${source}" >&2
    AE_CRI_IMAGE_MIRROR_ALWAYS_PULL=1 engine_pull "$source"
    ctr_delete_target_ref "retry-convert"
    ctr -n "$ctr_namespace" images convert --platform "$ctr_platform" "$source" "$target_image"
    return
  fi
  return 1
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
if [[ "$engine" == "ctr" && -z "$ctr_platform" ]]; then
  ctr_platform="$(default_ctr_platform)"
fi
echo "[cri-image-mirror] backend=${engine} source=${source_image} target=${target_image}"

engine_pull "$source_image"
if [[ "$engine" == "ctr" ]]; then
  ctr_prepare_push_ref "$source_image"
elif [[ "$source_image" != "$target_image" ]]; then
  engine_tag "$source_image" "$target_image"
fi
echo "[cri-image-mirror] pushing ${target_image}"
engine_push "$target_image"

if (( pull_cri == 1 )); then
  if ! command -v crictl >/dev/null 2>&1; then
    echo "crictl not found; cannot perform CRI pull verification" >&2
    exit 1
  fi
  if [[ "$engine" == "ctr" ]]; then
    ctr_refresh_cri_target "pre-pull"
  fi
  echo "[cri-image-mirror] CRI pull verify ${target_image}"
  crictl --runtime-endpoint "$cri_endpoint" pull "$target_image" >/dev/null
fi

echo "[cri-image-mirror] ready ${target_image}"
