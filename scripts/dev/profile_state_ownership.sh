#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${K1S_ROOT_DIR_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SELF_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

usage() {
  cat <<'USAGE'
Usage: scripts/dev/profile_state_ownership.sh --profile <name> [--check|--repair] [--target-uid <uid> --target-gid <gid>]

Options:
  --profile <name>  Profile name (for example: k1s-core)
  --check           Verify managed strict-CRI profile state is accessible to the target user
  --repair          Chown managed strict-CRI profile state to the target user
  --target-uid <n>  Override target uid used for repair/fallback checks
  --target-gid <n>  Override target gid used for repair/fallback checks
  -h, --help        Show this help
USAGE
}

profile=""
mode=""
target_uid_override=""
target_gid_override=""

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
    --target-uid)
      target_uid_override="${2:-}"
      shift
      ;;
    --target-gid)
      target_gid_override="${2:-}"
      shift
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

if [[ -n "$target_uid_override" || -n "$target_gid_override" ]]; then
  if [[ -z "$target_uid_override" || -z "$target_gid_override" ]]; then
    echo "error: --target-uid and --target-gid must be provided together" >&2
    exit 1
  fi
  if [[ ! "$target_uid_override" =~ ^[0-9]+$ ]]; then
    echo "error: --target-uid must be numeric: ${target_uid_override}" >&2
    exit 1
  fi
  if [[ ! "$target_gid_override" =~ ^[0-9]+$ ]]; then
    echo "error: --target-gid must be numeric: ${target_gid_override}" >&2
    exit 1
  fi
fi

resolve_target_ids() {
  if [[ -n "$target_uid_override" && -n "$target_gid_override" ]]; then
    printf '%s %s\n' "$target_uid_override" "$target_gid_override"
    return 0
  fi
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

filesystem_type() {
  local path="$1"
  stat -f -c %T "$path" 2>/dev/null || printf 'unknown'
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

run_check_as_target_user() {
  if [[ -z "${target_uid:-}" || ! "${target_uid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    return 1
  fi

  local -a cmd=(sudo -u "#${target_uid}")
  if [[ -n "${target_gid:-}" && "${target_gid}" =~ ^[0-9]+$ ]]; then
    # VM hostshare mounts can preserve a host-side numeric GID that is not mapped
    # inside the guest. In that case, probing access as the target UID is still
    # sufficient to verify usability after a non-chownable filesystem fallback.
    if ! command -v getent >/dev/null 2>&1 || getent group "${target_gid}" >/dev/null 2>&1; then
      cmd+=(-g "#${target_gid}")
    fi
  fi
  cmd+=(env "K1S_ROOT_DIR_OVERRIDE=${ROOT_DIR}")
  if [[ -n "${AE_TLS_DIR:-}" ]]; then
    cmd+=("AE_TLS_DIR=${AE_TLS_DIR}")
  fi
  cmd+=(bash "$SELF_SCRIPT" --profile "$profile" --check)
  "${cmd[@]}"
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
      if ! chown_err="$(chown -R "${target_uid}:${target_gid}" "$path" 2>&1)"; then
        fs_type="$(filesystem_type "$path")"
        if run_check_as_target_user; then
          echo "warning: ownership normalization skipped for ${path} on non-chownable filesystem (${fs_type}): ${chown_err}" >&2
          continue
        fi
        echo "error: failed to normalize strict-CRI profile state ownership for ${path} on filesystem ${fs_type}: ${chown_err}" >&2
        exit 1
      fi
    done < <(managed_paths)
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
