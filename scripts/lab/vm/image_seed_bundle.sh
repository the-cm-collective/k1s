#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

RUN_ID="${RUN_ID:-}"
MANIFEST="${MANIFEST:-$ROOT_DIR/lab/variants/cri_seed_images.lock.json}"
PROFILE="${PROFILE:-all}"
ENGINE="${ENGINE:-${AE_CRI_CACHE_SEED_ENGINE:-}}"
PLATFORM="${PLATFORM:-linux/amd64}"
ALWAYS_PULL="${AE_CRI_CACHE_SEED_ALWAYS_PULL:-0}"
CTR_NAMESPACE="${AE_CTR_NAMESPACE:-k8s.io}"
NERDCTL_NAMESPACE="${AE_NERDCTL_NAMESPACE:-k8s.io}"

usage() {
  cat <<USAGE
Usage: $0 --run-id <id> [options]

Build a host-side CRI seed image bundle for VM bootstrap cache import.

Options:
  --run-id <id>           Run id used for output path
  --manifest <path>       Seed manifest json (default: lab/variants/cri_seed_images.lock.json)
  --profile <name>        Image subset (core|edge|all, default: all)
  --output <path>         Bundle output tar path
  --engine <name>         Pull/export engine (docker|nerdctl|podman|ctr)
  --platform <value>      Pull platform (default: linux/amd64)
  --always-pull           Force remote pull even if source already cached
  -h, --help              Show this help

Environment:
  AE_CRI_CACHE_SEED_ALWAYS_PULL=1  force remote pull
  AE_CRI_CACHE_SEED_ENGINE          preferred engine (docker|nerdctl|podman|ctr)
  AE_CTR_NAMESPACE                  ctr namespace (default: k8s.io)
  AE_NERDCTL_NAMESPACE              nerdctl namespace (default: k8s.io)
USAGE
}

OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      RUN_ID="${2:?missing run id}"; shift 2 ;;
    --manifest)
      MANIFEST="${2:?missing manifest path}"; shift 2 ;;
    --profile)
      PROFILE="${2:?missing profile}"; shift 2 ;;
    --output)
      OUTPUT="${2:?missing output path}"; shift 2 ;;
    --engine)
      ENGINE="${2:?missing engine}"; shift 2 ;;
    --platform)
      PLATFORM="${2:?missing platform}"; shift 2 ;;
    --always-pull)
      ALWAYS_PULL=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "[cri-seed] unknown arg: $1" >&2
      usage
      exit 2 ;;
  esac
done

[[ -n "$RUN_ID" ]] || { echo "[cri-seed] --run-id is required" >&2; usage; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[cri-seed] manifest not found: $MANIFEST" >&2; exit 2; }

case "$PROFILE" in
  core|edge|all) ;;
  *)
    echo "[cri-seed] invalid --profile: $PROFILE (expected core|edge|all)" >&2
    exit 2
    ;;
esac

if ! command -v jq >/dev/null 2>&1; then
  echo "[cri-seed] jq is required" >&2
  exit 2
fi

seed_dir="$ROOT_DIR/state/lab-vm/$RUN_ID/seeds"
mkdir -p "$seed_dir"

if [[ -z "$OUTPUT" ]]; then
  OUTPUT="$seed_dir/cri-seed-images.oci.tar"
fi
meta_path="$seed_dir/cri-seed-info.json"
images_path="$seed_dir/cri-seed-images.txt"

resolve_engine() {
  if [[ -n "$ENGINE" ]]; then
    command -v "$ENGINE" >/dev/null 2>&1 || {
      echo "[cri-seed] requested engine not found: $ENGINE" >&2
      exit 2
    }
    return
  fi

  local candidate
  for candidate in docker nerdctl podman ctr; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ENGINE="$candidate"
      return
    fi
  done

  echo "[cri-seed] no supported engine found (docker|nerdctl|podman|ctr)" >&2
  exit 2
}

is_local_build_image() {
  local image="$1"
  [[ -n "$(local_build_context "$image")" ]]
}

local_build_context() {
  local image="$1"
  local normalized="$image"
  if [[ "$normalized" == */* ]]; then
    local first="${normalized%%/*}"
    if [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]; then
      normalized="${normalized#*/}"
    fi
  fi
  case "$normalized" in
    k1s-apishim:*|*/k1s-apishim:*) echo "__apishim__" ;;
    demo-shell:latest|*/demo-shell:latest) echo "$ROOT_DIR/samples/servers/shell-demo" ;;
    *)
      echo ""
      ;;
  esac
}

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

engine_has_image() {
  local image="$1"
  case "$ENGINE" in
    docker)
      docker image inspect "$image" >/dev/null 2>&1
      ;;
    nerdctl)
      nerdctl --namespace "$NERDCTL_NAMESPACE" image inspect "$image" >/dev/null 2>&1
      ;;
    podman)
      podman image exists "$image" >/dev/null 2>&1
      ;;
    ctr)
      ctr -n "$CTR_NAMESPACE" images ls -q 2>/dev/null | grep -Fx -- "$image" >/dev/null 2>&1
      ;;
    *)
      return 1
      ;;
  esac
}

engine_pull() {
  local image="$1"
  if ! is_truthy "$ALWAYS_PULL" && engine_has_image "$image"; then
    echo "[cri-seed] source already cached: $image"
    return
  fi

  case "$ENGINE" in
    docker)
      docker pull --platform "$PLATFORM" "$image"
      ;;
    nerdctl)
      nerdctl --namespace "$NERDCTL_NAMESPACE" pull --platform "$PLATFORM" "$image"
      ;;
    podman)
      podman pull --platform "$PLATFORM" "$image"
      ;;
    ctr)
      ctr -n "$CTR_NAMESPACE" images pull --platform "$PLATFORM" "$image"
      ;;
    *)
      echo "[cri-seed] unsupported engine: $ENGINE" >&2
      exit 2
      ;;
  esac
}

prepare_image() {
  local image="$1"
  local build_context=""
  build_context="$(local_build_context "$image")"
  if [[ -n "$build_context" ]]; then
    if [[ "$ENGINE" == "ctr" ]]; then
      echo "[cri-seed] engine=ctr cannot build local seed image: $image" >&2
      echo "[cri-seed] use docker, podman, or nerdctl for bundles that include repo-built images" >&2
      exit 2
    fi
    if ! is_truthy "$ALWAYS_PULL" && engine_has_image "$image"; then
      echo "[cri-seed] local seed image already cached: $image"
      return
    fi
    if [[ "$build_context" == "__apishim__" ]]; then
      local build_script="$ROOT_DIR/scripts/build_cri_apishim_image.sh"
      [[ -x "$build_script" ]] || {
        echo "[cri-seed] missing local image builder: $build_script" >&2
        exit 2
      }
      echo "[cri-seed] build local image: $image"
      bash "$build_script" \
        --engine "$ENGINE" \
        --image "$image" \
        --no-push \
        --no-pull-cri
      return
    fi
    [[ -d "$build_context" ]] || {
      echo "[cri-seed] missing local build context: $build_context" >&2
      exit 2
    }
    echo "[cri-seed] build local image: $image"
    case "$ENGINE" in
      docker)
        docker build --platform "$PLATFORM" -t "$image" "$build_context"
        ;;
      nerdctl)
        nerdctl --namespace "$NERDCTL_NAMESPACE" build --platform "$PLATFORM" -t "$image" "$build_context"
        ;;
      podman)
        podman build --platform "$PLATFORM" -t "$image" "$build_context"
        ;;
      *)
        echo "[cri-seed] unsupported engine for local build: $ENGINE" >&2
        exit 2
        ;;
    esac
    return
  fi
  engine_pull "$image"
}

engine_export() {
  local out="$1"
  shift
  local images=("$@")

  case "$ENGINE" in
    docker)
      docker save -o "$out" "${images[@]}"
      ;;
    nerdctl)
      nerdctl --namespace "$NERDCTL_NAMESPACE" save --format oci-archive -o "$out" "${images[@]}"
      ;;
    podman)
      podman save --format oci-archive -o "$out" "${images[@]}"
      ;;
    ctr)
      ctr -n "$CTR_NAMESPACE" images export "$out" "${images[@]}"
      ;;
    *)
      echo "[cri-seed] unsupported engine: $ENGINE" >&2
      exit 2
      ;;
  esac
}

read_images() {
  case "$PROFILE" in
    core)
      jq -r '.images.core[]?' "$MANIFEST"
      ;;
    edge)
      jq -r '.images.edge[]?' "$MANIFEST"
      ;;
    all)
      jq -r '[.images.core[]?, .images.edge[]?] | unique[]' "$MANIFEST"
      ;;
  esac
}

resolve_engine

mapfile -t images < <(read_images)
if [[ "${#images[@]}" -eq 0 ]]; then
  echo "[cri-seed] no images resolved from manifest=$MANIFEST profile=$PROFILE" >&2
  exit 2
fi

printf '%s\n' "${images[@]}" > "$images_path"

seed_version="$(jq -r '.seed_version // empty' "$MANIFEST")"
if [[ -z "$seed_version" ]]; then
  seed_version="$(sha256sum "$MANIFEST" | awk '{print substr($1,1,16)}')"
fi

platform="$(jq -r '.platform // empty' "$MANIFEST")"
if [[ -z "$platform" ]]; then
  platform="$PLATFORM"
fi

archive_format="oci-archive"
if [[ "$ENGINE" == "docker" ]]; then
  archive_format="docker-archive"
fi

echo "[cri-seed] run_id=$RUN_ID engine=$ENGINE profile=$PROFILE platform=$platform"
echo "[cri-seed] manifest=$MANIFEST seed_version=$seed_version"

for image in "${images[@]}"; do
  echo "[cri-seed] ensure image: $image"
  prepare_image "$image"
done

tmp_output="${OUTPUT}.tmp"
rm -f "$tmp_output"

echo "[cri-seed] exporting bundle: $tmp_output"
engine_export "$tmp_output" "${images[@]}"
mv "$tmp_output" "$OUTPUT"

jq -n \
  --arg run_id "$RUN_ID" \
  --arg manifest "$MANIFEST" \
  --arg seed_version "$seed_version" \
  --arg profile "$PROFILE" \
  --arg engine "$ENGINE" \
  --arg platform "$platform" \
  --arg archive_format "$archive_format" \
  --arg bundle "$OUTPUT" \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson images "$(jq -R -s 'split("\n") | map(select(length>0))' "$images_path")" \
  '{
    run_id:$run_id,
    manifest:$manifest,
    seed_version:$seed_version,
    profile:$profile,
    engine:$engine,
    platform:$platform,
    archive_format:$archive_format,
    bundle:$bundle,
    generated_at:$generated_at,
    images:$images
  }' > "$meta_path"

echo "[cri-seed] bundle ready: $OUTPUT"
echo "[cri-seed] metadata: $meta_path"
