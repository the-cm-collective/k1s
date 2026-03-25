#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NIXOS_MODULE_DEST="${AE_NIXOS_MODULE_DEST:-/etc/nixos/nixos/modules/k1s-local-dev-bridge.nix}"
NIXOS_BRIDGE_ROOT="${AE_NIXOS_BRIDGE_ROOT:-/var/lib/k1s-dev}"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

print_header() {
  printf '[env-doctor] %s\n' "$1"
}

print_kv() {
  local key="$1"
  local value="$2"
  printf '%-28s %s\n' "$key" "$value"
}

tool_status() {
  local label="$1"
  local cmd="$2"
  local resolved=""
  resolved="$(command -v "$cmd" 2>/dev/null || true)"
  if [[ -n "$resolved" ]]; then
    print_kv "$label" "ok (${resolved})"
  else
    print_kv "$label" "missing"
  fi
}

compose_first_useful_line() {
  awk '
    /^>>>> Executing external compose provider / { next }
    /^Please see podman-compose\(1\)/ { next }
    /^<<</ { next }
    NF { print; exit }
  '
}

compose_status() {
  local label="$1"
  shift
  local output=""
  output="$("$@" 2>&1)" && {
    local detail=""
    detail="$(printf '%s\n' "$output" | compose_first_useful_line)"
    if [[ -n "$detail" ]]; then
      print_kv "$label" "ok (${detail})"
    else
      print_kv "$label" "ok"
    fi
    return 0
  }
  if [[ -n "$output" ]]; then
    print_kv "$label" "unavailable ($(printf '%s\n' "$output" | compose_first_useful_line))"
  else
    print_kv "$label" "unavailable"
  fi
}

socket_status() {
  local label="$1"
  local path="$2"
  if [[ -S "$path" ]]; then
    print_kv "$label" "ok (${path})"
  else
    print_kv "$label" "missing (${path})"
  fi
}

service_status() {
  local label="$1"
  shift
  local state="unknown"
  if "$@" >/dev/null 2>&1; then
    state="$("$@" 2>/dev/null || true)"
  fi
  if [[ -z "$state" ]]; then
    state="unknown"
  fi
  print_kv "$label" "$state"
}

resolve_status() {
  local host="$1"
  local resolved=""
  if resolved="$(getent hosts "$host" 2>/dev/null | awk '{print $1}' | paste -sd, -)" && [[ -n "$resolved" ]]; then
    print_kv "$host" "$resolved"
  else
    print_kv "$host" "unresolved"
  fi
}

os_id() {
  if [[ -r /etc/os-release ]]; then
    awk -F= '/^ID=/{gsub(/"/, "", $2); print $2; exit}' /etc/os-release
    return 0
  fi
  printf '%s' "unknown"
}

nixos_bridge_imported() {
  if [[ ! -d /etc/nixos || ! -f "$NIXOS_MODULE_DEST" ]]; then
    return 1
  fi
  rg -l --glob '*.nix' 'k1s-local-dev-bridge' /etc/nixos 2>/dev/null | grep -F -v "$NIXOS_MODULE_DEST" >/dev/null 2>&1
}

path_status() {
  local label="$1"
  local path="$2"
  if [[ -e "$path" ]]; then
    print_kv "$label" "present (${path})"
  else
    print_kv "$label" "missing (${path})"
  fi
}

find_apishim_env_file() {
  local candidate=""
  for candidate in \
    "${AE_APISHIM_ENV_FILE:-}" \
    "$ROOT_DIR/state/profiles/demo/apishim.env" \
    "$ROOT_DIR/state/profiles/labs/apishim.env"; do
    if [[ -n "$candidate" && -f "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

compose_render_status() {
  local label="$1"
  local engine="$2"
  local dev_env="$ROOT_DIR/state/dev.env"
  local env_file=""
  local profile_dir=""
  local output_file=""
  local first_line=""

  if ! has_cmd "$engine"; then
    print_kv "$label" "${engine} missing"
    return 0
  fi
  if ! "$engine" compose version >/dev/null 2>&1; then
    print_kv "$label" "provider unavailable"
    return 0
  fi
  if [[ ! -f "$dev_env" ]]; then
    print_kv "$label" "skipped (missing ${dev_env})"
    return 0
  fi
  env_file="$(find_apishim_env_file || true)"
  if [[ -z "$env_file" ]]; then
    print_kv "$label" "skipped (missing apishim env file)"
    return 0
  fi
  profile_dir="$(dirname "$env_file")"
  if [[ "$profile_dir" == "$ROOT_DIR/"* ]]; then
    profile_dir="${profile_dir#"$ROOT_DIR/"}"
  fi
  output_file="$(mktemp)"
  if APISHIM_ENV_FILE="$env_file" \
    APISHIM_PROFILE_DIR="$profile_dir" \
    APISHIM_PORT="${APISHIM_PORT:-8445}" \
    APISHIM_HOST_PORT="${APISHIM_HOST_PORT:-${APISHIM_PORT:-8445}}" \
    APISHIM_CONTAINER_PORT="${APISHIM_CONTAINER_PORT:-8445}" \
    AE_APISHIM_ETCD_ENDPOINTS="${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}" \
    "$engine" compose -f "$ROOT_DIR/ops/dev/docker-compose.yaml" config >"$output_file" 2>&1; then
    print_kv "$label" "ok (${profile_dir})"
    rm -f "$output_file"
    return 0
  fi
  first_line="$(compose_first_useful_line <"$output_file")"
  [[ -z "$first_line" ]] && first_line="render failed"
  print_kv "$label" "failed (${first_line})"
  rm -f "$output_file"
}

print_header "repo"
print_kv "root" "$ROOT_DIR"
print_kv "os" "$(os_id)"
print_kv "nix shell" "${IN_NIX_SHELL:-no}"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  print_kv ".venv" "active (${VIRTUAL_ENV})"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  print_kv ".venv" "present ($ROOT_DIR/.venv)"
else
  print_kv ".venv" "missing"
fi

print_header "toolchain"
tool_status "python" python
tool_status "ruff" ruff
tool_status "mypy" mypy
tool_status "pytest" pytest
tool_status "pre-commit" pre-commit
tool_status "podman" podman
tool_status "podman-compose" podman-compose
tool_status "docker" docker
tool_status "openssl" openssl
tool_status "sqlite3" sqlite3
tool_status "sops" sops
tool_status "age" age
tool_status "curl" curl
tool_status "jq" jq
tool_status "containerd" containerd
tool_status "crictl" crictl
tool_status "iptables" iptables
tool_status "certutil" certutil
tool_status "nixos-rebuild" nixos-rebuild

print_header "compose"
if has_cmd podman; then
  compose_status "podman compose" podman compose version
  compose_render_status "podman apishim render" podman
else
  print_kv "podman compose" "podman missing"
  print_kv "podman apishim render" "podman missing"
fi
if has_cmd docker; then
  compose_status "docker compose" docker compose version
  compose_render_status "docker apishim render" docker
else
  print_kv "docker compose" "docker missing"
  print_kv "docker apishim render" "docker missing"
fi

print_header "host services"
if has_cmd systemctl; then
  service_status "podman.socket (user)" systemctl --user is-active podman.socket
  service_status "containerd.service" systemctl is-active containerd
else
  print_kv "systemctl" "missing"
fi
socket_status "podman rootful socket" "/run/podman/podman.sock"
socket_status "podman rootless socket" "/run/user/$(id -u)/podman/podman.sock"
socket_status "containerd socket" "/run/containerd/containerd.sock"

print_header "local dns / trust"
path_status "combined dev CA" "${ROOT_DIR}/state/certs/combined-dev-ca.pem"
path_status "caddy local root" "${ROOT_DIR}/state/certs/caddy-local-root.crt"
path_status "bridge hosts" "${NIXOS_BRIDGE_ROOT}/extra-hosts"
path_status "bridge cert dir" "${NIXOS_BRIDGE_ROOT}/certs"

if [[ "$(os_id)" == "nixos" ]]; then
  if [[ -f "$NIXOS_MODULE_DEST" ]]; then
    print_kv "nixos bridge module" "present (${NIXOS_MODULE_DEST})"
  else
    print_kv "nixos bridge module" "missing (${NIXOS_MODULE_DEST})"
  fi
  if nixos_bridge_imported; then
    print_kv "nixos bridge import" "ok"
  else
    print_kv "nixos bridge import" "missing"
  fi
fi

if has_cmd update-ca-certificates; then
  print_kv "system trust updater" "update-ca-certificates"
elif has_cmd update-ca-trust; then
  print_kv "system trust updater" "update-ca-trust"
else
  print_kv "system trust updater" "missing"
fi

resolve_status "docs.home.arpa"
resolve_status "api.home.arpa"
resolve_status "dash.home.arpa"
resolve_status "blue.home.arpa"
resolve_status "green.home.arpa"

print_header "next steps"
print_kv "default shell" "nix develop"
print_kv "cri shell" "nix develop .#cri"
print_kv "bootstrap" "python -m venv .venv && . .venv/bin/activate && python -m pip install -e .[dev]"
print_kv "apply helper" "make dev-local"
print_kv "cleanup helper" "make dev-local-clean"
