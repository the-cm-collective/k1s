#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/dev/examples_registry_prepare.sh [options]

Build/mirror example images into an OCI registry and optionally write rewritten manifests.

Options:
  --registry <host:port>    Target registry (default: AE_CRI_REGISTRY or AE_REGISTRY_HOST)
  --namespace <prefix>      Optional registry path prefix (default: AE_CRI_REGISTRY_NAMESPACE)
  --engine <name>           Build/push backend (nerdctl|podman|docker)
  --source-dir <path>       Source dir containing example manifests (repeatable)
  --output-dir <path>       Output dir for rewritten manifests (default: state/examples/registry)
  --no-rewrite              Skip writing rewritten manifests
  --pull-cri                Prewarm each pushed image via CRI pull
  --cri-endpoint <uri>      CRI endpoint for --pull-cri (default: AE_CRI_ENDPOINT)
  -h, --help                Show this help

Notes:
  - Defaults source dirs to specs/examples and docs/site/examples (if present).
  - Local demo images are built from samples/servers:
      demo-shell:latest -> samples/servers/shell-demo
      demo-green:latest -> samples/servers/green
      demo-blue:latest  -> samples/servers/blue
USAGE
}

registry="${AE_CRI_REGISTRY:-${AE_REGISTRY_HOST:-}}"
namespace="${AE_CRI_REGISTRY_NAMESPACE:-}"
engine=""
rewrite=1
pull_cri=0
output_dir="state/examples/registry"
cri_endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
declare -a source_dirs=()
declare -a manifest_files=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)
      registry="${2:?missing registry host}"; shift ;;
    --namespace)
      namespace="${2:?missing namespace prefix}"; shift ;;
    --engine)
      engine="${2:?missing engine}"; shift ;;
    --source-dir)
      source_dirs+=("${2:?missing source dir}"); shift ;;
    --output-dir)
      output_dir="${2:?missing output dir}"; shift ;;
    --no-rewrite)
      rewrite=0 ;;
    --pull-cri)
      pull_cri=1 ;;
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

if [[ -z "$registry" ]]; then
  echo "Registry is required. Set AE_CRI_REGISTRY/AE_REGISTRY_HOST or pass --registry." >&2
  exit 1
fi

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

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|y|Y) return 0 ;;
    *) return 1 ;;
  esac
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

has_registry_prefix() {
  local ref="$1"
  [[ "$ref" == */* ]] || return 1
  local first="${ref%%/*}"
  [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]
}

registry_ref() {
  local reg="$1"
  local src="$2"
  local ns="$3"
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
  if [[ -n "$ns" ]]; then
    name="${ns%/}/${name#*/}"
  fi
  echo "${reg%/}/${name#*/}${suffix}${digest}"
}

local_build_context() {
  local img="$1"
  local normalized="$img"
  if has_registry_prefix "$normalized"; then
    normalized="${normalized#*/}"
  fi
  case "$normalized" in
    demo-shell:latest)
      echo "${root_dir}/samples/servers/shell-demo"
      ;;
    demo-green:latest)
      echo "${root_dir}/samples/servers/green"
      ;;
    demo-blue:latest)
      echo "${root_dir}/samples/servers/blue"
      ;;
    *)
      ;;
  esac
}

resolve_engine
echo "[examples-registry] backend=${engine} registry=${registry} namespace=${namespace:-<none>}"

if [[ ${#source_dirs[@]} -eq 0 ]]; then
  [[ -d "${root_dir}/specs/examples" ]] && source_dirs+=("${root_dir}/specs/examples")
  [[ -d "${root_dir}/docs/site/examples" ]] && source_dirs+=("${root_dir}/docs/site/examples")
fi
if [[ ${#source_dirs[@]} -eq 0 ]]; then
  echo "No source directories found." >&2
  exit 1
fi

for dir in "${source_dirs[@]}"; do
  local_dir="$dir"
  if [[ "$local_dir" != /* ]]; then
    local_dir="${root_dir}/${local_dir}"
  fi
  if [[ ! -d "$local_dir" ]]; then
    echo "[examples-registry] skipping missing source dir: $local_dir"
    continue
  fi
  while IFS= read -r -d '' file; do
    manifest_files+=("$file")
  done < <(find "$local_dir" -type f \( -name '*.yaml' -o -name '*.yml' \) -print0)
done

if [[ ${#manifest_files[@]} -eq 0 ]]; then
  echo "No example manifests found in source directories." >&2
  exit 1
fi

images_file="$(mktemp -t k1s-example-images-XXXXXX.txt)"
mapping_file="$(mktemp -t k1s-example-map-XXXXXX.txt)"
trap 'rm -f "$images_file" "$mapping_file"' EXIT

python3 - "$images_file" "${manifest_files[@]}" <<'PY'
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])
files = [Path(p) for p in sys.argv[2:]]
pattern = re.compile(r"^\s*image:\s*['\"]?([^'\"\s#]+)")
images = set()
for path in files:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            images.add(m.group(1).strip())
out.write_text("\n".join(sorted(images)) + ("\n" if images else ""), encoding="utf-8")
PY

if [[ ! -s "$images_file" ]]; then
  echo "No image references found in manifests." >&2
  exit 1
fi

while IFS= read -r img; do
  [[ -z "$img" ]] && continue
  target="$(registry_ref "$registry" "$img" "$namespace")"
  context="$(local_build_context "$img" || true)"
  if [[ -n "$context" ]]; then
    if [[ ! -f "$context/Dockerfile" ]]; then
      echo "[examples-registry] missing Dockerfile for local image '$img' at $context" >&2
      exit 1
    fi
    echo "[examples-registry] build $img -> $target"
    "$engine" build -t "$target" "$context"
  else
    echo "[examples-registry] mirror $img -> $target"
    engine_pull "$img"
    "$engine" tag "$img" "$target"
  fi
  engine_push "$target"
  if (( pull_cri == 1 )); then
    if ! command -v crictl >/dev/null 2>&1; then
      echo "crictl not found; cannot prewarm CRI images" >&2
      exit 1
    fi
    crictl --runtime-endpoint "$cri_endpoint" pull "$target" >/dev/null
  fi
  printf '%s\t%s\n' "$img" "$target" >>"$mapping_file"
done < "$images_file"

if (( rewrite == 1 )); then
  out_abs="$output_dir"
  if [[ "$out_abs" != /* ]]; then
    out_abs="${root_dir}/${out_abs}"
  fi
  rm -rf "$out_abs"
  mkdir -p "$out_abs"
  for src in "${manifest_files[@]}"; do
    rel="${src#${root_dir}/}"
    dst="${out_abs}/${rel}"
    mkdir -p "$(dirname "$dst")"
    python3 - "$src" "$dst" "$mapping_file" <<'PY'
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
mapping_file = Path(sys.argv[3])
mapping = {}
for line in mapping_file.read_text(encoding="utf-8").splitlines():
    if not line.strip() or "\t" not in line:
        continue
    s, t = line.split("\t", 1)
    mapping[s.strip()] = t.strip()

pattern = re.compile(r"^(\s*image:\s*)(['\"]?)([^'\"\s#]+)(['\"]?)(\s*(?:#.*)?)$")
out_lines = []
for raw in src.read_text(encoding="utf-8").splitlines(keepends=True):
    line = raw.rstrip("\n")
    m = pattern.match(line)
    if m:
        image = m.group(3).strip()
        if image in mapping:
            quote = m.group(2) or m.group(4) or ""
            line = f"{m.group(1)}{quote}{mapping[image]}{quote}{m.group(5)}"
    out_lines.append(line + ("\n" if raw.endswith("\n") else ""))
dst.write_text("".join(out_lines), encoding="utf-8")
PY
  done
  echo "[examples-registry] wrote rewritten manifests to ${out_abs}"
fi

echo "[examples-registry] done. mirrored images:"
cat "$mapping_file"
