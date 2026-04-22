#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ROOT_DIR
OUT_DIR="${1:-${RELEASE_ARTIFACT_DIR:-$ROOT_DIR/dist/release-images}}"
mkdir -p "$OUT_DIR"

REF_NAME="${RELEASE_REF_NAME:-${GITHUB_REF_NAME:-}}"
if [[ -z "$REF_NAME" ]]; then
  REF_NAME="v$(python - <<'PY'
import os
import tomllib
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
PY
)"
fi
REVISION="${RELEASE_REVISION:-${GITHUB_SHA:-$(git -C "$ROOT_DIR" rev-parse HEAD)}}"
SOURCE_URL="${RELEASE_SOURCE_URL:-}"
if [[ -z "$SOURCE_URL" && -n "${GITHUB_SERVER_URL:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
  SOURCE_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}"
fi
if [[ -z "$SOURCE_URL" ]]; then
  SOURCE_URL="$(git config --get remote.origin.url || true)"
fi
CREATED="${RELEASE_CREATED:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
PLATFORM="${RELEASE_PLATFORM:-linux/amd64}"
ALIASES_CSV="${RELEASE_IMAGE_ALIASES:-k1s-core,k1s-core-node,k1s-edge-core,k1s-edge-node}"
BUILDER_NAME="${RELEASE_BUILDER_NAME:-k1s-release-builder}"
BUILDER_CONTEXT_NAME="${RELEASE_BUILDER_CONTEXT:-${BUILDER_NAME}-context}"

case "$REF_NAME" in
  v*) VERSION_LABEL="${REF_NAME#v}" ;;
  *) VERSION_LABEL="$REF_NAME" ;;
esac

ref_slug="${REF_NAME//\//-}"
platform_slug="${PLATFORM//\//-}"
manifest_json="$OUT_DIR/manifest.json"
sha_file="$OUT_DIR/SHA256SUMS"
manifest_tmp="$(mktemp)"
trap 'rm -f "$manifest_tmp"' EXIT
: >"$manifest_tmp"
: >"$sha_file"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required to build OCI release artifacts" >&2
  exit 2
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "error: docker buildx is required to build OCI release artifacts" >&2
  exit 2
fi

configure_builder_context() {
  if [[ -z "${DOCKER_HOST:-}" ]]; then
    return 0
  fi
  if [[ -z "${DOCKER_TLS_VERIFY:-}" && -z "${DOCKER_CERT_PATH:-}" && -z "${DOCKER_TLS_CERTDIR:-}" ]]; then
    return 0
  fi

  local cert_path="${DOCKER_CERT_PATH:-}"
  if [[ -z "$cert_path" && -n "${DOCKER_TLS_CERTDIR:-}" && -d "${DOCKER_TLS_CERTDIR}/client" ]]; then
    cert_path="${DOCKER_TLS_CERTDIR}/client"
  fi

  local -a docker_opts=("host=${DOCKER_HOST}")
  if [[ -n "$cert_path" ]]; then
    [[ -f "$cert_path/ca.pem" ]] && docker_opts+=("ca=$cert_path/ca.pem")
    [[ -f "$cert_path/cert.pem" ]] && docker_opts+=("cert=$cert_path/cert.pem")
    [[ -f "$cert_path/key.pem" ]] && docker_opts+=("key=$cert_path/key.pem")
  fi
  if [[ "${DOCKER_TLS_VERIFY:-}" == "0" ]]; then
    docker_opts+=("skip-tls-verify=true")
  fi

  local docker_spec
  docker_spec="$(IFS=,; printf '%s' "${docker_opts[*]}")"
  if docker context inspect "$BUILDER_CONTEXT_NAME" >/dev/null 2>&1; then
    docker context update "$BUILDER_CONTEXT_NAME" --docker "$docker_spec" >/dev/null
  else
    docker context create "$BUILDER_CONTEXT_NAME" --description "k1s release builder context" --docker "$docker_spec" >/dev/null
  fi

  # Buildx refuses docker-container builders when TLS connection data only
  # exists in env vars; use an explicit Docker context instead.
  export DOCKER_CONTEXT="$BUILDER_CONTEXT_NAME"
  unset DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_TLS_CERTDIR
}

configure_builder_context

if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  if [[ -n "${DOCKER_CONTEXT:-}" ]]; then
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --use "$DOCKER_CONTEXT" >/dev/null
  else
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --use >/dev/null
  fi
else
  docker buildx use "$BUILDER_NAME" >/dev/null
fi
docker buildx inspect "$BUILDER_NAME" --bootstrap >/dev/null

role_for_alias() {
  case "$1" in
    k1s-core) printf '%s' "controller" ;;
    k1s-core-node|k1s-edge-node) printf '%s' "node" ;;
    k1s-edge-core) printf '%s' "gateway" ;;
    *)
      echo "error: unknown release image alias: $1" >&2
      exit 2
      ;;
  esac
}

dockerfile_for_role() {
  case "$1" in
    controller) printf '%s' "$ROOT_DIR/ops/images/controller.Dockerfile" ;;
    node) printf '%s' "$ROOT_DIR/ops/images/node.Dockerfile" ;;
    gateway) printf '%s' "$ROOT_DIR/ops/images/gateway.Dockerfile" ;;
    *)
      echo "error: unknown release image role: $1" >&2
      exit 2
      ;;
  esac
}

title_for_role() {
  case "$1" in
    controller) printf '%s' "k1s controller role image" ;;
    node) printf '%s' "k1s node agent role image" ;;
    gateway) printf '%s' "k1s gateway role image" ;;
    *)
      echo "error: unknown release image role: $1" >&2
      exit 2
      ;;
  esac
}

description_for_alias() {
  case "$1" in
    k1s-core) printf '%s' "Role-backed k1s-core release image alias for the controller runtime." ;;
    k1s-core-node) printf '%s' "Role-backed k1s-core-node release image alias for the node agent runtime." ;;
    k1s-edge-core) printf '%s' "Role-backed k1s-edge-core release image alias for the gateway runtime." ;;
    k1s-edge-node) printf '%s' "Role-backed k1s-edge-node release image alias for the node agent runtime." ;;
    *)
      echo "error: unknown release image alias: $1" >&2
      exit 2
      ;;
  esac
}

validate_oci_archive() {
  local archive="$1"
  if ! tar -tf "$archive" | grep -qx 'oci-layout'; then
    echo "error: $archive is missing oci-layout" >&2
    exit 1
  fi
  if ! tar -tf "$archive" | grep -qx 'index.json'; then
    echo "error: $archive is missing index.json" >&2
    exit 1
  fi
}

IFS=',' read -r -a aliases <<<"$ALIASES_CSV"
for alias in "${aliases[@]}"; do
  alias="$(printf '%s' "$alias" | xargs)"
  [[ -n "$alias" ]] || continue
  role="$(role_for_alias "$alias")"
  dockerfile="$(dockerfile_for_role "$role")"
  title="$(title_for_role "$role")"
  description="$(description_for_alias "$alias")"
  image_ref="k1s/${alias}:${ref_slug}"
  archive_name="${alias}-${ref_slug}-${platform_slug}.oci.tar"
  archive_path="$OUT_DIR/$archive_name"

  docker buildx build \
    --platform "$PLATFORM" \
    --provenance=false \
    --label "org.opencontainers.image.title=$title" \
    --label "org.opencontainers.image.description=$description" \
    --label "org.opencontainers.image.version=$VERSION_LABEL" \
    --label "org.opencontainers.image.revision=$REVISION" \
    --label "org.opencontainers.image.source=$SOURCE_URL" \
    --label "org.opencontainers.image.created=$CREATED" \
    --label "org.opencontainers.image.licenses=Apache-2.0" \
    --label "io.k1s.image.role=$role" \
    --label "io.k1s.image.alias=$alias" \
    --file "$dockerfile" \
    --output "type=oci,dest=$archive_path,name=$image_ref" \
    "$ROOT_DIR"

  validate_oci_archive "$archive_path"
  sha256sum "$archive_path" >>"$sha_file"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$alias" \
    "$role" \
    "$image_ref" \
    "$PLATFORM" \
    "$archive_name" \
    "$VERSION_LABEL" \
    "$REVISION" >>"$manifest_tmp"
done

{
  echo "{"
  echo "  \"artifacts\": ["
  row_num=0
  while IFS=$'\t' read -r alias role image_ref platform archive_name version revision; do
    [[ -n "$alias" ]] || continue
    row_num=$((row_num + 1))
    sha="$(awk -v target="$archive_name" '$2 == target || $2 ~ "/" target "$" {print $1}' "$sha_file" | head -n1)"
    if [[ "$row_num" -gt 1 ]]; then
      echo "    ,{"
    else
      echo "    {"
    fi
    printf '      "alias": "%s",\n' "$alias"
    printf '      "role": "%s",\n' "$role"
    printf '      "image_ref": "%s",\n' "$image_ref"
    printf '      "platform": "%s",\n' "$platform"
    printf '      "archive": "%s",\n' "$archive_name"
    printf '      "version": "%s",\n' "$version"
    printf '      "revision": "%s",\n' "$revision"
    printf '      "sha256": "%s"\n' "$sha"
    echo "    }"
  done <"$manifest_tmp"
  echo "  ]"
  echo "}"
} >"$manifest_json"
