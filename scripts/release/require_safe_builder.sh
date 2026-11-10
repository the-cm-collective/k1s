#!/usr/bin/env bash

k1s_release_builder_error() {
  printf 'error: %s\n' "$*" >&2
  return 2
}

k1s_release_require_safe_builder() {
  if [[ "${K1S_RELEASE_BUILDER_SCOPE:-}" != "isolated-ci" ]]; then
    k1s_release_builder_error \
      "K1S_RELEASE_BUILDER_SCOPE=isolated-ci is required; refusing an unscoped release builder"
    return 2
  fi

  case "${DOCKER_HOST:-}" in
    tcp://*) ;;
    ""|unix://*|npipe://*)
      k1s_release_builder_error \
        "a dedicated remote Docker builder is required; host-local Docker sockets are forbidden"
      return 2
      ;;
    *)
      k1s_release_builder_error \
        "DOCKER_HOST must select a dedicated remote tcp:// release builder"
      return 2
      ;;
  esac

  if [[ "${DOCKER_TLS_VERIFY:-}" != "1" ]]; then
    k1s_release_builder_error \
      "DOCKER_TLS_VERIFY=1 is required for the dedicated release builder"
    return 2
  fi

  local cert_path="${DOCKER_CERT_PATH:-}"
  if [[ -z "$cert_path" && -n "${DOCKER_TLS_CERTDIR:-}" ]]; then
    cert_path="${DOCKER_TLS_CERTDIR%/}/client"
  fi
  if [[ -z "$cert_path" ]]; then
    k1s_release_builder_error \
      "DOCKER_CERT_PATH or DOCKER_TLS_CERTDIR is required for the release builder"
    return 2
  fi

  local cert_file
  for cert_file in ca.pem cert.pem key.pem; do
    if [[ ! -f "$cert_path/$cert_file" ]]; then
      k1s_release_builder_error \
        "release builder TLS material is incomplete: missing $cert_file"
      return 2
    fi
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  k1s_release_require_safe_builder
fi
