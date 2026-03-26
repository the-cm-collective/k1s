#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${K1S_ROOT_DIR_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/profile_state_ownership.sh --profile <name> [--check|--repair]

Options:
  --profile <name>  Profile name (for example: k1s-core)
  --check           Verify managed strict-CRI profile state is accessible to the target user
  --repair          Chown managed strict-CRI profile state to the target user
  -h, --help        Show this help
USAGE
}

profile=""
mode=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      profile="${2:-}"
      shift
      ;;
    --check)
      mode="check"
      ;;
    --repair)
      mode="repair"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ -z "$profile" || -z "$mode" ]]; then
  usage >&2
  exit 1
fi

resolve_target_ids() {
  if [[ -n "${SUDO_UID:-}" && "${SUDO_UID}" =~ ^[0-9]+$ ]]; then
    local gid="${SUDO_GID:-}"
    if [[ -z "$gid" && -n "${SUDO_USER:-}" ]]; then
      gid="$(id -g "${SUDO_USER}" 2>/dev/null || true)"
    fi
    printf '%s %s\n' "${SUDO_UID}" "${gid:-$(id -g)}"
    return 0
  fi
  printf '%s %s\n' "$(id -u)" "$(id -g)"
}

resolve_tls_root() {
  local tls_root="${AE_TLS_DIR:-$ROOT_DIR/state/tls}"
  if [[ "$tls_root" != /* ]]; then
    tls_root="${ROOT_DIR}/${tls_root}"
  fi
  printf '%s\n' "$tls_root"
}

managed_paths() {
  local profile_dir="$ROOT_DIR/state/profiles/$profile"
  local tls_root
  local entry
  tls_root="$(resolve_tls_root)"

  if [[ -d "$profile_dir" ]]; then
    while IFS= read -r -d '' entry; do
      local base
      base="$(basename "$entry")"
      case "$base" in
        .|..|cri-data)
          continue
          ;;
        registry)
          if [[ -e "$entry/tls" ]]; then
            printf '%s\0' "$entry/tls"
          fi
          continue
          ;;
      esac
      printf '%s\0' "$entry"
    done < <(find "$profile_dir" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
  fi

  for entry in "$tls_root/envoy-fallback.crt" "$tls_root/envoy-fallback.key"; do
    if [[ -e "$entry" ]]; then
      printf '%s\0' "$entry"
    fi
  done
}

check_access_issue() {
  local path="$1"
  local item=""

  if [[ ! -r "$path" ]]; then
    printf '%s (not readable)\n' "$path"
    return 0
  fi
  if [[ ! -w "$path" ]]; then
    printf '%s (not writable)\n' "$path"
    return 0
  fi
  if [[ -d "$path" && ! -x "$path" ]]; then
    printf '%s (not searchable)\n' "$path"
    return 0
  fi
  if [[ ! -d "$path" ]]; then
    return 1
  fi

  while IFS= read -r -d '' item; do
    if [[ ! -r "$item" ]]; then
      printf '%s (not readable)\n' "$item"
      return 0
    fi
    if [[ ! -w "$item" ]]; then
      printf '%s (not writable)\n' "$item"
      return 0
    fi
    if [[ -d "$item" && ! -x "$item" ]]; then
      printf '%s (not searchable)\n' "$item"
      return 0
    fi
  done < <(find "$path" -mindepth 1 -maxdepth 4 -print0 2>/dev/null)

  return 1
}

read -r target_uid target_gid < <(resolve_target_ids)

case "$mode" in
  check)
    while IFS= read -r -d '' path; do
      if issue="$(check_access_issue "$path")"; then
        echo "managed strict-CRI profile state requires repair: ${issue}" >&2
        exit 1
      fi
    done < <(managed_paths)
    ;;
  repair)
    if [[ "$(id -u)" -ne 0 ]]; then
      echo "repair requires root so ownership can be restored to uid=${target_uid} gid=${target_gid}" >&2
      exit 1
    fi
    while IFS= read -r -d '' path; do
      chown -R "${target_uid}:${target_gid}" "$path"
    done < <(managed_paths)
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
