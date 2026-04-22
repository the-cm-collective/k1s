#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/cni_bin_bootstrap.sh

Bootstrap the expected CNI plugin directory for strict-CRI/containerd flows.

Environment:
  CNI_BIN_DIR                  Destination plugin dir (default: /opt/cni/bin)
  AE_CNI_REQUIRED_PLUGINS      CSV plugin list (default: bridge,portmap,firewall,tuning,loopback)
  AE_CNI_BOOTSTRAP_SOURCE_DIRS Colon-separated source dirs to check first
                               (default: /usr/lib/cni:/usr/local/lib/cni:/run/current-system/sw/bin)
  AE_CNI_BOOTSTRAP_MODE        symlink|copy (default: symlink)
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

cni_bin="${CNI_BIN_DIR:-/opt/cni/bin}"
required_raw="${AE_CNI_REQUIRED_PLUGINS:-bridge,portmap,firewall,tuning,loopback}"
source_dirs_raw="${AE_CNI_BOOTSTRAP_SOURCE_DIRS:-/usr/lib/cni:/usr/local/lib/cni:/run/current-system/sw/bin}"
bootstrap_mode="$(printf '%s' "${AE_CNI_BOOTSTRAP_MODE:-symlink}" | tr '[:upper:]' '[:lower:]')"

case "$bootstrap_mode" in
  symlink|copy) ;;
  *)
    echo "unsupported AE_CNI_BOOTSTRAP_MODE: $bootstrap_mode" >&2
    exit 1
    ;;
esac

need_sudo=0
if [[ ! -d "$cni_bin" ]]; then
  parent_dir="$(dirname "$cni_bin")"
  if [[ ! -d "$parent_dir" || ! -w "$parent_dir" ]]; then
    need_sudo=1
  fi
elif [[ ! -w "$cni_bin" ]]; then
  need_sudo=1
fi

if [[ $need_sudo -eq 1 && $EUID -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to write to $cni_bin" >&2
    exit 1
  fi
fi

run_cmd() {
  if [[ $need_sudo -eq 1 && $EUID -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

log() {
  printf '[cni-bootstrap] %s\n' "$1"
}

resolve_realpath() {
  local path="$1"
  if command -v readlink >/dev/null 2>&1; then
    readlink -f "$path" 2>/dev/null || printf '%s\n' "$path"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$path" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
    return 0
  fi
  printf '%s\n' "$path"
}

looks_like_cni_plugin_path() {
  local path="$1"
  [[ "$path" == *"/cni/"* || "$path" == *"cni-plugins"* || "$path" == /run/current-system/sw/bin/* ]]
}

split_required_plugins() {
  local raw="$1"
  local normalized
  normalized="$(printf '%s' "$raw" | tr ',' ' ')"
  printf '%s\n' $normalized
}

resolve_plugin_source() {
  local plugin="$1"
  local dir="" candidate="" resolved=""

  IFS=':' read -r -a source_dirs <<<"$source_dirs_raw"
  for dir in "${source_dirs[@]}"; do
    [[ -n "$dir" ]] || continue
    candidate="${dir%/}/${plugin}"
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if ! command -v "$plugin" >/dev/null 2>&1; then
    return 1
  fi
  candidate="$(command -v "$plugin")"
  resolved="$(resolve_realpath "$candidate")"
  if looks_like_cni_plugin_path "$candidate" || looks_like_cni_plugin_path "$resolved"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

install_plugin() {
  local src="$1"
  local dst="$2"
  if [[ "$bootstrap_mode" == "copy" ]]; then
    run_cmd install -m 0755 "$src" "$dst"
    return 0
  fi
  run_cmd ln -sfn "$src" "$dst"
}

run_cmd mkdir -p "$cni_bin"

declare -a missing_plugins=()
declare -a installed_plugins=()

while IFS= read -r plugin; do
  [[ -n "$plugin" ]] || continue
  dst="${cni_bin%/}/${plugin}"
  if [[ -x "$dst" ]]; then
    continue
  fi
  if ! src="$(resolve_plugin_source "$plugin")"; then
    missing_plugins+=("$plugin")
    continue
  fi
  install_plugin "$src" "$dst"
  installed_plugins+=("$plugin")
done < <(split_required_plugins "$required_raw")

if [[ "${#missing_plugins[@]}" -gt 0 ]]; then
  echo "missing required CNI plugins: ${missing_plugins[*]}" >&2
  echo "looked in: ${source_dirs_raw} and PATH candidates matching cni plugin locations" >&2
  exit 1
fi

if [[ "${#installed_plugins[@]}" -gt 0 ]]; then
  log "bootstrapped CNI plugins into ${cni_bin}: ${installed_plugins[*]}"
else
  log "CNI plugins already ready in ${cni_bin}"
fi
