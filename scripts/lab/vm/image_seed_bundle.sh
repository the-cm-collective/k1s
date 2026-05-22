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
METADATA_ONLY=0

usage() {
  cat <<USAGE
Usage: $0 --run-id <id> [options]

Build a host-side CRI seed image bundle for VM bootstrap cache import.

Options:
  --run-id <id>           Run id used for output path
  --manifest <path>       Seed manifest json (default: lab/variants/cri_seed_images.lock.json)
  --profile <name>        Image subset (bootstrap|core|edge|all, default: all)
  --output <path>         Bundle output tar path
  --engine <name>         Pull/export engine (docker|nerdctl|podman|ctr)
  --platform <value>      Pull platform (default: linux/amd64)
  --metadata-only         Prepare images and emit metadata without exporting a bundle
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
    --metadata-only)
      METADATA_ONLY=1; shift ;;
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
  bootstrap|core|edge|all) ;;
  *)
    echo "[cri-seed] invalid --profile: $PROFILE (expected bootstrap|core|edge|all)" >&2
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
image_meta_path="$seed_dir/cri-seed-image-metadata.jsonl"

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
    k1s-vllm-openai:*|*/k1s-vllm-openai:*) echo "__host_a_vllm__" ;;
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
    if [[ "$build_context" == "__host_a_vllm__" ]]; then
      echo "[cri-seed] host-a vLLM image must be prebuilt before validation seed export: $image" >&2
      echo "[cri-seed] run scripts/build_cri_host_a_vllm_image.sh --image $image" >&2
      exit 2
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
      if [[ "${#images[@]}" -gt 1 ]]; then
        podman save --multi-image-archive --format docker-archive -o "$out" "${images[@]}"
      else
        podman save --format oci-archive -o "$out" "${images[@]}"
      fi
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

engine_image_id() {
  local image="$1"
  local value=""
  case "$ENGINE" in
    docker)
      value="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
      ;;
    podman)
      value="$(podman image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
      ;;
    nerdctl)
      value="$(
        nerdctl --namespace "$NERDCTL_NAMESPACE" image inspect "$image" 2>/dev/null \
          | jq -r '.[0].Id // .[0].ID // .Id // .ID // empty' 2>/dev/null || true
      )"
      ;;
    ctr)
      value="$(
        ctr -n "$CTR_NAMESPACE" images inspect --content "$image" 2>/dev/null \
          | jq -r '.config.digest // .target.digest // .digest // empty' 2>/dev/null || true
      )"
      if [[ -z "$value" ]]; then
        value="$(
          ctr -n "$CTR_NAMESPACE" images inspect "$image" 2>/dev/null \
            | jq -r '.config.digest // .target.digest // .digest // empty' 2>/dev/null || true
        )"
      fi
      ;;
    *)
      value=""
      ;;
  esac
  printf '%s' "${value//$'\n'/}"
}

canonical_image_id() {
  local value="${1:-}"
  value="${value//$'\n'/}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  if [[ "$value" =~ ^sha256:([0-9a-f]{64})$ ]]; then
    printf 'sha256:%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  if [[ "$value" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'sha256:%s' "$value"
    return 0
  fi
  return 1
}

archive_image_id() {
  local archive="$1"
  local image="$2"
  local format="$3"
  local value=""

  case "$format" in
    oci-archive)
      value="$(
        python3 - "$archive" "$image" <<'PY'
import json
import subprocess
import sys

archive, image = sys.argv[1:3]

def tar_read(path: str) -> str:
    proc = subprocess.run(
        ["tar", "-xOf", archive, path],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(1)
    return proc.stdout

index = json.loads(tar_read("index.json"))
digest = ""
for manifest in index.get("manifests", []):
    annotations = manifest.get("annotations") or {}
    if annotations.get("org.opencontainers.image.ref.name") == image:
        digest = str(manifest.get("digest") or "")
        break
if not digest.startswith("sha256:"):
    raise SystemExit(1)
manifest = json.loads(tar_read(f"blobs/sha256/{digest.removeprefix('sha256:')}"))
config_digest = str((manifest.get("config") or {}).get("digest") or "").strip()
if not config_digest:
    raise SystemExit(1)
print(config_digest)
PY
      )"
      ;;
    docker-archive)
      value="$(
        python3 - "$archive" "$image" <<'PY'
import hashlib
import json
import subprocess
import sys

archive, image = sys.argv[1:3]

def tar_read(path: str) -> bytes:
    proc = subprocess.run(
        ["tar", "-xOf", archive, path],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(1)
    return proc.stdout

manifest_entries = json.loads(tar_read("manifest.json").decode("utf-8"))
config_path = ""
for entry in manifest_entries:
    repo_tags = entry.get("RepoTags") or []
    if image in repo_tags:
        config_path = str(entry.get("Config") or "").strip()
        break
if not config_path:
    raise SystemExit(1)
config_bytes = tar_read(config_path)
print("sha256:" + hashlib.sha256(config_bytes).hexdigest())
PY
      )"
      ;;
    *)
      value=""
      ;;
  esac

  printf '%s' "${value//$'\n'/}"
}

rewrite_archive_image_metadata() {
  local archive="$1"
  local format="$2"
  local tmp_meta="${image_meta_path}.tmp"
  local image=""
  local provided_image_id=""
  local resolved_image_id=""
  local archive_resolved_image_id=""

  : > "$tmp_meta"
  for entry_json in "${image_entries[@]}"; do
    image="$(jq -r '.ref' <<<"$entry_json")"
    provided_image_id="$(jq -r '.expected_image_id // empty' <<<"$entry_json")"
    archive_resolved_image_id="$(canonical_image_id "$(archive_image_id "$archive" "$image" "$format")" || true)"
    resolved_image_id="$archive_resolved_image_id"
    if [[ -z "$resolved_image_id" ]]; then
      resolved_image_id="$(canonical_image_id "$(engine_image_id "$image")" || true)"
    fi
    if [[ -z "$resolved_image_id" ]]; then
      resolved_image_id="$(canonical_image_id "$provided_image_id" || true)"
    fi
    jq -cn \
      --arg ref "$image" \
      --arg expected_image_id "$resolved_image_id" \
      'if ($expected_image_id | length) > 0 then {ref:$ref, expected_image_id:$expected_image_id} else {ref:$ref} end' \
      >> "$tmp_meta"
  done
  mv "$tmp_meta" "$image_meta_path"
}

resolve_archive_format() {
  case "$ENGINE" in
    docker)
      echo "docker-archive"
      ;;
    podman)
      if [[ "${#images[@]}" -gt 1 ]]; then
        echo "docker-archive"
      else
        echo "oci-archive"
      fi
      ;;
    *)
      echo "oci-archive"
      ;;
  esac
}

read_image_entries() {
  case "$PROFILE" in
    bootstrap)
      jq -c '.images.bootstrap[]? | if type == "string" then {ref: ., expected_image_id: null} elif type == "object" then {ref: (.ref // ""), expected_image_id: (.expected_image_id // null)} else empty end | select((.ref | type) == "string" and (.ref | length) > 0)' "$MANIFEST"
      ;;
    core)
      jq -c '.images.core[]? | if type == "string" then {ref: ., expected_image_id: null} elif type == "object" then {ref: (.ref // ""), expected_image_id: (.expected_image_id // null)} else empty end | select((.ref | type) == "string" and (.ref | length) > 0)' "$MANIFEST"
      ;;
    edge)
      jq -c '.images.edge[]? | if type == "string" then {ref: ., expected_image_id: null} elif type == "object" then {ref: (.ref // ""), expected_image_id: (.expected_image_id // null)} else empty end | select((.ref | type) == "string" and (.ref | length) > 0)' "$MANIFEST"
      ;;
    all)
      jq -c '[.images.bootstrap[]?, .images.core[]?, .images.edge[]?] | map(if type == "string" then {ref: ., expected_image_id: null} elif type == "object" then {ref: (.ref // ""), expected_image_id: (.expected_image_id // null)} else empty end) | map(select((.ref | type) == "string" and (.ref | length) > 0)) | unique_by(.ref)[]' "$MANIFEST"
      ;;
  esac
}

resolve_engine

mapfile -t image_entries < <(read_image_entries)
if [[ "${#image_entries[@]}" -eq 0 ]]; then
  echo "[cri-seed] no images resolved from manifest=$MANIFEST profile=$PROFILE" >&2
  exit 2
fi

images=()
: > "$images_path"
: > "$image_meta_path"

seed_version="$(jq -r '.seed_version // empty' "$MANIFEST")"
if [[ -z "$seed_version" ]]; then
  seed_version="$(sha256sum "$MANIFEST" | awk '{print substr($1,1,16)}')"
fi

platform="$(jq -r '.platform // empty' "$MANIFEST")"
if [[ -z "$platform" ]]; then
  platform="$PLATFORM"
fi

echo "[cri-seed] run_id=$RUN_ID engine=$ENGINE profile=$PROFILE platform=$platform"
echo "[cri-seed] manifest=$MANIFEST seed_version=$seed_version"

for entry_json in "${image_entries[@]}"; do
  image="$(jq -r '.ref' <<<"$entry_json")"
  provided_image_id="$(jq -r '.expected_image_id // empty' <<<"$entry_json")"
  echo "[cri-seed] ensure image: $image"
  prepare_image "$image"
  images+=("$image")
  printf '%s\n' "$image" >> "$images_path"
  resolved_image_id="$(canonical_image_id "$(engine_image_id "$image")" || true)"
  if [[ -z "$resolved_image_id" ]]; then
    resolved_image_id="$(canonical_image_id "$provided_image_id" || true)"
  fi
  jq -cn \
    --arg ref "$image" \
    --arg expected_image_id "$resolved_image_id" \
    'if ($expected_image_id | length) > 0 then {ref:$ref, expected_image_id:$expected_image_id} else {ref:$ref} end' \
    >> "$image_meta_path"
done
printf '%s\n' "${images[@]}" > "$images_path"

archive_format="$(resolve_archive_format)"

if [[ "$METADATA_ONLY" != "1" ]]; then
  tmp_output="${OUTPUT}.tmp"
  rm -f "$tmp_output"

  echo "[cri-seed] exporting bundle: $tmp_output"
  engine_export "$tmp_output" "${images[@]}"
  mv "$tmp_output" "$OUTPUT"
  rewrite_archive_image_metadata "$OUTPUT" "$archive_format"
fi

jq -n \
  --arg run_id "$RUN_ID" \
  --arg manifest "$MANIFEST" \
  --arg seed_version "$seed_version" \
  --arg profile "$PROFILE" \
  --arg engine "$ENGINE" \
  --arg platform "$platform" \
  --arg archive_format "$archive_format" \
  --arg bundle "$OUTPUT" \
  --argjson metadata_only "$(if [[ "$METADATA_ONLY" == "1" ]]; then echo true; else echo false; fi)" \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson image_refs "$(jq -R -s 'split("\n") | map(select(length>0))' "$images_path")" \
  --argjson images "$(jq -s '.' "$image_meta_path")" \
  '{
    run_id:$run_id,
    manifest:$manifest,
    seed_version:$seed_version,
    profile:$profile,
    engine:$engine,
    platform:$platform,
    archive_format:$archive_format,
    bundle:$bundle,
    metadata_only:$metadata_only,
    generated_at:$generated_at,
    image_refs:$image_refs,
    images:$images
  }' > "$meta_path"

if [[ "$METADATA_ONLY" == "1" ]]; then
  echo "[cri-seed] metadata-only ready: $meta_path"
else
  echo "[cri-seed] bundle ready: $OUTPUT"
fi
echo "[cri-seed] metadata: $meta_path"
