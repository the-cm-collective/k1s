#!/usr/bin/env bash
set -e

usage() {
  cat <<'EOF'
Usage:
  source scripts/ae-env-clear.sh
  eval "$(./scripts/ae-env-clear.sh --print)"
  ./scripts/ae-env-clear.sh --print

--print: Emit unset lines (for eval/source).
EOF
}

emit_unsets() {
  cat <<'EOF'
unset \
  AE_API_SERVER \
  AE_APISHIM_SERVER \
  AE_STATE_DB \
  AE_STATE_BACKEND \
  AE_ETCD_ENDPOINTS \
  AE_ETCD_PREFIX \
  AE_PROJECTION_ROOT \
  AE_SPECS_DIR \
  AE_APISHIM_DB \
  AE_APISHIM_RUNTIME \
  AE_APISHIM_MIRROR \
  AE_APISHIM_SOT \
  AE_APISHIM_TOKEN \
  AE_APISHIM_READ_TOKEN \
  AE_APISHIM_SESSION_SECRET \
  AE_APISHIM_EXEC_TOKEN \
  AE_APISHIM_PORTFORWARD_TOKEN \
  AE_API_ADMIN_TOKEN \
  AE_API_SCALER_TOKEN \
  AE_API_READ_TOKEN \
  AE_API_MUTATIONS \
  AE_LABS_TOKEN \
  AE_LABS_HELM_TOKEN \
  AE_LABS_HELM_SERVER \
  AE_CADDY_SITES \
  AE_CADDY_FILE \
  AE_CADDY_CONTAINER \
  AE_CONTAINER_CLI \
  AE_DOCKER_NETWORK \
  AE_PODMAN_NETWORK
EOF
}

apply_unsets() {
  # Unset common k1s/ae environment variables that leak between profiles.
  unset \
    AE_API_SERVER \
    AE_APISHIM_SERVER \
    AE_STATE_DB \
    AE_STATE_BACKEND \
    AE_ETCD_ENDPOINTS \
    AE_ETCD_PREFIX \
    AE_PROJECTION_ROOT \
    AE_SPECS_DIR \
    AE_APISHIM_DB \
    AE_APISHIM_RUNTIME \
    AE_APISHIM_MIRROR \
    AE_APISHIM_SOT \
    AE_APISHIM_TOKEN \
    AE_APISHIM_READ_TOKEN \
    AE_APISHIM_SESSION_SECRET \
    AE_APISHIM_EXEC_TOKEN \
    AE_APISHIM_PORTFORWARD_TOKEN \
    AE_API_ADMIN_TOKEN \
    AE_API_SCALER_TOKEN \
    AE_API_READ_TOKEN \
    AE_API_MUTATIONS \
    AE_LABS_TOKEN \
    AE_LABS_HELM_TOKEN \
    AE_LABS_HELM_SERVER \
    AE_CADDY_SITES \
    AE_CADDY_FILE \
    AE_CADDY_CONTAINER \
    AE_CONTAINER_CLI \
    AE_DOCKER_NETWORK \
    AE_PODMAN_NETWORK
}

is_sourced=0
if [[ -n "${BASH_SOURCE[0]-}" && "${BASH_SOURCE[0]}" != "${0}" ]]; then
  is_sourced=1
elif [[ -n "${ZSH_EVAL_CONTEXT-}" && "${ZSH_EVAL_CONTEXT}" == *:file* ]]; then
  is_sourced=1
fi

finish() {
  local code="${1:-0}"
  if [[ "$is_sourced" -eq 1 ]]; then
    return "$code"
  fi
  exit "$code"
}

mode="apply"
if [[ $# -gt 0 ]]; then
  case "$1" in
    --print)
      mode="print"
      ;;
    --help|-h)
      usage
      finish 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      finish 2
      ;;
  esac
fi

if [[ "$mode" == "print" ]]; then
  emit_unsets
  finish 0
fi

if [[ "$is_sourced" -eq 0 ]]; then
  echo "[ae-env-clear] This script must be sourced to affect the current shell." >&2
  echo "Use: source scripts/ae-env-clear.sh" >&2
  echo "Or:  eval \"\$(./scripts/ae-env-clear.sh --print)\"" >&2
  finish 2
fi

apply_unsets
echo "[ae-env-clear] cleared AE_* profile/auth variables from this shell"
