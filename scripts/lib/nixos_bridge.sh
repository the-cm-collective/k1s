#!/usr/bin/env bash

k1s_nixos_bridge_root() {
  printf '%s' "${AE_NIXOS_BRIDGE_ROOT:-/var/lib/k1s-dev}"
}

k1s_nixos_search_root() {
  printf '%s' "${AE_NIXOS_SEARCH_ROOT:-/etc/nixos}"
}

k1s_nixos_bridge_hosts_file() {
  printf '%s/extra-hosts' "$(k1s_nixos_bridge_root)"
}

k1s_nixos_bridge_cert_dir() {
  printf '%s/certs' "$(k1s_nixos_bridge_root)"
}

k1s_nixos_module_dest() {
  printf '%s' "${AE_NIXOS_MODULE_DEST:-/etc/nixos/nixos/modules/k1s-local-dev-bridge.nix}"
}

k1s_nixos_cri_module_dest() {
  printf '%s' "${AE_NIXOS_CRI_MODULE_DEST:-/etc/nixos/nixos/modules/k1s-cri-host.nix}"
}

k1s_nixos_flake() {
  printf '%s' "${AE_NIXOS_FLAKE:-/etc/nixos}"
}

k1s_nixos_host() {
  printf '%s' "${AE_NIXOS_HOST:-$(hostname -s 2>/dev/null || hostname)}"
}

k1s_nixos_rebuild_mode() {
  printf '%s' "${AE_NIXOS_REBUILD:-prompt}"
}

k1s_os_id() {
  if [[ -n "${K1S_OS_ID_OVERRIDE:-}" ]]; then
    printf '%s' "${K1S_OS_ID_OVERRIDE}"
    return 0
  fi
  local os_release="${1:-/etc/os-release}"
  if [[ ! -r "$os_release" ]]; then
    return 1
  fi
  sed -n 's/^ID=//p' "$os_release" | head -n1 | tr -d '"' | tr -d "'"
}

k1s_is_nixos() {
  [[ "$(k1s_os_id "${1:-/etc/os-release}" 2>/dev/null || true)" == "nixos" ]]
}

_k1s_nixos_module_imported() {
  local search_root="${1:-$(k1s_nixos_search_root)}"
  local module_dest="$2"
  local marker="$3"
  if [[ ! -d "$search_root" ]]; then
    return 1
  fi
  if [[ ! -f "$module_dest" ]]; then
    return 1
  fi
  if command -v rg >/dev/null 2>&1; then
    rg -l --glob '*.nix' "$marker" "$search_root" 2>/dev/null | \
      grep -F -v "$module_dest" >/dev/null 2>&1
    return $?
  fi
  grep -R -l --include='*.nix' "$marker" "$search_root" 2>/dev/null | \
    grep -F -v "$module_dest" >/dev/null 2>&1
}

k1s_nixos_bridge_imported() {
  _k1s_nixos_module_imported \
    "${1:-$(k1s_nixos_search_root)}" \
    "${2:-$(k1s_nixos_module_dest)}" \
    'k1s-local-dev-bridge'
}

k1s_nixos_cri_module_imported() {
  _k1s_nixos_module_imported \
    "${1:-$(k1s_nixos_search_root)}" \
    "${2:-$(k1s_nixos_cri_module_dest)}" \
    'k1s-cri-host'
}

k1s_nixos_rebuild_ref() {
  printf '%s#%s' "$(k1s_nixos_flake)" "$(k1s_nixos_host)"
}

_k1s_nixos_bootstrap_instructions() {
  local module_src="${1:?module source required}"
  local module_dest="${2:?module dest required}"
  local import_ref="${3:?import ref required}"
  local flake="${4:-$(k1s_nixos_flake)}"
  local host="${5:-$(k1s_nixos_host)}"
  printf '  sudo install -D -m 0644 %s %s\n' "$module_src" "$module_dest"
  printf '  add %s to your host imports under %s\n' "$import_ref" "$flake"
  printf '  sudo nixos-rebuild switch --impure --flake %s#%s\n' "$flake" "$host"
}

k1s_nixos_bootstrap_instructions() {
  local root_dir="${1:?root dir required}"
  local module_dest="${2:-$(k1s_nixos_module_dest)}"
  local flake="${3:-$(k1s_nixos_flake)}"
  local host="${4:-$(k1s_nixos_host)}"
  _k1s_nixos_bootstrap_instructions \
    "${root_dir}/ops/nixos/k1s-local-dev-bridge.nix" \
    "$module_dest" \
    "./nixos/modules/k1s-local-dev-bridge.nix" \
    "$flake" \
    "$host"
}

k1s_nixos_cri_bootstrap_instructions() {
  local root_dir="${1:?root dir required}"
  local module_dest="${2:-$(k1s_nixos_cri_module_dest)}"
  local flake="${3:-$(k1s_nixos_flake)}"
  local host="${4:-$(k1s_nixos_host)}"
  _k1s_nixos_bootstrap_instructions \
    "${root_dir}/ops/nixos/k1s-cri-host.nix" \
    "$module_dest" \
    "./nixos/modules/k1s-cri-host.nix" \
    "$flake" \
    "$host"
}

k1s_containerd_cni_env() {
  local containerd_bin="${CONTAINERD_BIN:-containerd}"
  local python_bin="${PYTHON_BIN:-}"
  local config_dump=""

  if [[ -z "$python_bin" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      python_bin="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
      python_bin="$(command -v python)"
    else
      return 1
    fi
  fi

  if ! command -v "$containerd_bin" >/dev/null 2>&1; then
    return 1
  fi
  if ! config_dump="$("$containerd_bin" config dump 2>/dev/null)"; then
    return 1
  fi

  CONTAINERD_CONFIG_DUMP="$config_dump" "$python_bin" - <<'PY'
import os
import shlex
import sys

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit(1)

raw = os.environ.get("CONTAINERD_CONFIG_DUMP", "")
if not raw.strip():
    raise SystemExit(1)

try:
    data = tomllib.loads(raw)
except Exception:
    raise SystemExit(1)

plugins = data.get("plugins") or {}
if not isinstance(plugins, dict):
    raise SystemExit(1)

candidates = []
for key in ("io.containerd.cri.v1.runtime", "io.containerd.grpc.v1.cri"):
    entry = plugins.get(key) or {}
    if not isinstance(entry, dict):
        continue
    cni = entry.get("cni") or {}
    if isinstance(cni, dict):
        candidates.append(cni)

bin_dir = ""
conf_dir = ""
for cni in candidates:
    bin_dirs = cni.get("bin_dirs")
    if isinstance(bin_dirs, list) and not bin_dir:
      for item in bin_dirs:
          value = str(item or "").strip()
          if value:
              bin_dir = value
              break
    if not bin_dir:
        value = str(cni.get("bin_dir") or "").strip()
        if value:
            bin_dir = value
    if not conf_dir:
        value = str(cni.get("conf_dir") or "").strip()
        if value:
            conf_dir = value
    if bin_dir and conf_dir:
        break

if not bin_dir and not conf_dir:
    raise SystemExit(1)

if bin_dir:
    print(f"export CNI_BIN_DIR={shlex.quote(bin_dir)}")
if conf_dir:
    print(f"export CNI_CONF_DIR={shlex.quote(conf_dir)}")
PY
}
