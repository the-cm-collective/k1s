#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AE_BIN="${AE_BIN:-}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
READ_ONLY=0
AUTH_ARGS=()

usage() {
  cat <<'USAGE'
Usage: scripts/dev/apishim_kubectl.sh [options] -- <kubectl args>
       scripts/dev/apishim_kubectl.sh [options] <kubectl args>

Authenticates against the local API shim with `ae auth local --strict`, then
executes kubectl using the exported server/token settings.

Options:
  --read-only            Use AE_APISHIM_READ_TOKEN instead of AE_APISHIM_TOKEN
  --ae-bin <path>        Path to ae CLI (default: repo .venv/bin/ae or PATH ae)
  --kubectl-bin <path>   Path to kubectl (default: kubectl on PATH)
  --apishim-env <path>   Forwarded to `ae auth local --strict`
  --controller-env <p>   Forwarded to `ae auth local --strict`
  --server <url>         Forwarded to `ae auth local --strict`
  -h, --help             Show this help text
USAGE
}

die() {
  printf '[apishim-kubectl] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --read-only)
      READ_ONLY=1
      shift
      ;;
    --ae-bin)
      AE_BIN="${2:-}"
      shift 2
      ;;
    --kubectl-bin)
      KUBECTL_BIN="${2:-}"
      shift 2
      ;;
    --apishim-env|--controller-env|--server)
      AUTH_ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

[[ $# -gt 0 ]] || die "kubectl arguments are required"

if [[ -z "$AE_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/ae" ]]; then
    AE_BIN="$ROOT_DIR/.venv/bin/ae"
  elif command -v ae >/dev/null 2>&1; then
    AE_BIN="$(command -v ae)"
  else
    die "'ae' command not found"
  fi
fi

command -v "$KUBECTL_BIN" >/dev/null 2>&1 || die "'$KUBECTL_BIN' command not found"

auth_exports="$("$AE_BIN" auth local --strict "${AUTH_ARGS[@]}")"
eval "$auth_exports"

token="${AE_APISHIM_TOKEN:-}"
if [[ "$READ_ONLY" == "1" ]]; then
  token="${AE_APISHIM_READ_TOKEN:-}"
fi

[[ -n "${AE_APISHIM_SERVER:-}" ]] || die "AE_APISHIM_SERVER was not exported by strict auth"
[[ -n "$token" ]] || die "API shim token was not exported by strict auth"

ca_args=(--insecure-skip-tls-verify)
if [[ -n "${AE_APISHIM_CA_BUNDLE:-}" && -f "${AE_APISHIM_CA_BUNDLE}" ]]; then
  ca_args=(--certificate-authority "${AE_APISHIM_CA_BUNDLE}")
fi

exec "$KUBECTL_BIN" \
  --kubeconfig=/dev/null \
  --server "${AE_APISHIM_SERVER}" \
  --token "${token}" \
  "${ca_args[@]}" \
  "$@"
