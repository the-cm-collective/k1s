#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${ROOT_DIR}/scripts/lib/nixos_bridge.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/containerd_registry_trust.sh [options]

Options:
  --host <host:port>   Registry host (required)
  --ca <path>          CA cert path (required for https unless --insecure)
  --scheme <http|https>  Scheme (default: https)
  --insecure           Skip TLS verification (writes skip_verify = true)
  --podman-root        Install CA for rootful Podman under /etc/containers/certs.d
  --podman-user-home <path>  Install CA for rootless Podman under <path>/.config/containers/certs.d
  --docker             Install CA for Docker under /etc/docker/certs.d
  --system-trust       Install CA into system trust store (Linux/NixOS bridge)
  --restart            Restart containerd after writing hosts.toml
  -h, --help           Show this help

Examples:
  scripts/containerd_registry_trust.sh --host registry.k1s.home.arpa:32000 --ca /tmp/registry.crt
  scripts/containerd_registry_trust.sh --host registry.k1s.home.arpa:32000 --ca /tmp/registry.crt --system-trust --restart
USAGE
}

host=""
ca=""
scheme="https"
insecure=0
podman_root=0
podman_user_home=""
docker_trust=0
system_trust=0
restart=0

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      host="${2:?missing host}"; shift ;;
    --ca)
      ca="${2:?missing ca path}"; shift ;;
    --scheme)
      scheme="${2:?missing scheme}"; shift ;;
    --insecure)
      insecure=1 ;;
    --podman-root)
      podman_root=1 ;;
    --podman-user-home)
      podman_user_home="${2:?missing podman user home}"; shift ;;
    --docker)
      docker_trust=1 ;;
    --system-trust)
      system_trust=1 ;;
    --restart)
      restart=1 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage; exit 2 ;;
  esac
  shift
 done

if [[ -z "$host" ]]; then
  echo "--host is required" >&2
  exit 1
fi

if [[ "$scheme" != "http" && "$scheme" != "https" ]]; then
  echo "--scheme must be http or https" >&2
  exit 1
fi

if [[ "$scheme" == "https" && $insecure -eq 0 && -z "$ca" ]]; then
  echo "--ca is required for https (or use --insecure)" >&2
  exit 1
fi

if [[ $system_trust -eq 1 && -z "$ca" ]]; then
  echo "--ca is required for --system-trust" >&2
  exit 1
fi

CONTAINERD_CONFIG_FILE="${K1S_CONTAINERD_CONFIG_FILE:-/etc/containerd/config.toml}"
CONTAINERD_CERTS_DIR_ROOT="${K1S_CONTAINERD_CERTS_DIR_ROOT:-/etc/containerd/certs.d}"
PODMAN_CERTS_DIR_ROOT="${K1S_PODMAN_CERTS_DIR_ROOT:-/etc/containers/certs.d}"
DOCKER_CERTS_DIR_ROOT="${K1S_DOCKER_CERTS_DIR_ROOT:-/etc/docker/certs.d}"
ALLOW_UNPRIVILEGED=0
if is_truthy "${K1S_REGISTRY_TRUST_ALLOW_UNPRIVILEGED:-0}"; then
  ALLOW_UNPRIVILEGED=1
fi

SUDO=""
if [[ "$EUID" -ne 0 && "$ALLOW_UNPRIVILEGED" -eq 0 ]]; then
  SUDO="sudo"
fi
HAS_TTY=0
if [[ -t 0 && -t 1 ]]; then
  HAS_TTY=1
fi

log() {
  printf '[registry-trust] %s\n' "$1"
}

warn() {
  printf '[registry-trust] warning: %s\n' "$1" >&2
}

prompt_yes_no() {
  local msg="$1"
  local default="${2:-Y}"
  local prompt="" reply=""
  if [[ "$default" == "Y" || "$default" == "y" ]]; then
    prompt="$msg [Y/n]: "
  else
    prompt="$msg [y/N]: "
  fi
  read -r -p "$prompt" reply || reply=""
  if [[ -z "$reply" ]]; then
    reply="$default"
  fi
  case "$reply" in
    Y|y|Yes|yes) return 0 ;;
    *) return 1 ;;
  esac
}

run_priv() {
  if [[ "$EUID" -eq 0 || "$ALLOW_UNPRIVILEGED" -eq 1 ]]; then
    "$@"
    return 0
  fi
  sudo "$@"
}

ensure_containerd_config_version_v2() {
  local config_file="$CONTAINERD_CONFIG_FILE"
  [[ -f "$config_file" ]] || return 1

  # Only normalize when file already uses v2-style plugin identifiers.
  if ! grep -Eq 'io\.containerd\.grpc\.v1\.cri|io\.containerd\.[[:alnum:]_.-]+\.v[0-9]+' "$config_file"; then
    return 1
  fi

  local current
  current="$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' "$config_file" | head -n1)"
  if [[ "$current" == "2" ]]; then
    return 1
  fi

  local tmp
  tmp="$(mktemp)"
  if [[ -n "$current" ]]; then
    awk '
      BEGIN { done=0 }
      {
        if (!done && $0 ~ /^[[:space:]]*version[[:space:]]*=/) {
          print "version = 2"
          done=1
          next
        }
        print
      }
    ' "$config_file" >"$tmp"
    if ! grep -Eq '^[[:space:]]*version[[:space:]]*=' "$tmp"; then
      {
        echo 'version = 2'
        echo
        cat "$tmp"
      } >"${tmp}.withver"
      mv "${tmp}.withver" "$tmp"
    fi
  else
    {
      echo 'version = 2'
      echo
      cat "$config_file"
    } >"$tmp"
  fi

  if cmp -s "$config_file" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi

  $SUDO cp "$tmp" "$config_file"
  $SUDO chmod 0644 "$config_file"
  rm -f "$tmp"
  echo "containerd config normalized: ${config_file} (version=2)"
  return 0
}

ensure_containerd_registry_config_path() {
  local config_file="$CONTAINERD_CONFIG_FILE"
  local desired="$CONTAINERD_CERTS_DIR_ROOT"

  if [[ ! -f "$config_file" ]]; then
    return 1
  fi

  local tmp
  tmp="$(mktemp)"
  awk -v desired="$desired" '
    BEGIN { in_table=0; done=0 }
    {
      if ($0 ~ /^[[:space:]]*\[plugins\."io\.containerd\.grpc\.v1\.cri"\.registry\][[:space:]]*$/) {
        print
        in_table=1
        next
      }
      if (in_table && $0 ~ /^[[:space:]]*\[/) {
        if (!done) {
          print "  config_path = \"" desired "\""
          done=1
        }
        in_table=0
      }
      if (in_table && $0 ~ /^[[:space:]]*config_path[[:space:]]*=/) {
        if (!done) {
          print "  config_path = \"" desired "\""
          done=1
        }
        next
      }
      print
    }
    END {
      if (in_table && !done) {
        print "  config_path = \"" desired "\""
        done=1
      }
      if (!done) {
        if (NR > 0) {
          print ""
        }
        print "[plugins.\"io.containerd.grpc.v1.cri\".registry]"
        print "  config_path = \"" desired "\""
      }
    }
  ' "$config_file" >"$tmp"

  if cmp -s "$config_file" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi

  $SUDO cp "$tmp" "$config_file"
  $SUDO chmod 0644 "$config_file"
  rm -f "$tmp"
  echo "containerd CRI registry config updated: ${config_file} (config_path=${desired})"
  return 0
}

ensure_containerd_runc_runtime() {
  local config_file="$CONTAINERD_CONFIG_FILE"
  [[ -f "$config_file" ]] || return 1

  if grep -Eq '^[[:space:]]*\[plugins\."io\.containerd\.grpc\.v1\.cri"\.containerd\.runtimes\.runc\][[:space:]]*$' "$config_file"; then
    return 1
  fi

  local tmp
  tmp="$(mktemp)"
  cat "$config_file" >"$tmp"
  {
    echo
    echo '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]'
    echo '  runtime_type = "io.containerd.runc.v2"'
  } >>"$tmp"

  if cmp -s "$config_file" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi

  $SUDO cp "$tmp" "$config_file"
  $SUDO chmod 0644 "$config_file"
  rm -f "$tmp"
  echo "containerd CRI runtime updated: ensured runc handler in ${config_file}"
  return 0
}

sync_nixos_bridge_trust() {
  local trust_name="$1"
  local changed=0
  local cert_dir dst
  cert_dir="$(k1s_nixos_bridge_cert_dir)"
  dst="${cert_dir}/${trust_name}"

  run_priv mkdir -p "$cert_dir"
  if [[ ! -f "$dst" ]] || ! cmp -s "$ca" "$dst"; then
    run_priv install -m 0644 "$ca" "$dst"
    changed=1
  fi
  if [[ "$changed" -eq 0 ]]; then
    return 0
  fi

  local module_dest flake host rebuild_ref mode
  module_dest="$(k1s_nixos_module_dest)"
  flake="$(k1s_nixos_flake)"
  host="$(k1s_nixos_host)"
  rebuild_ref="$(k1s_nixos_rebuild_ref)"
  mode="$(k1s_nixos_rebuild_mode)"

  if ! k1s_nixos_bridge_imported /etc/nixos "$module_dest"; then
    warn "NixOS bridge not detected; install it once before expecting persistent system CA updates"
    k1s_nixos_bootstrap_instructions "$ROOT_DIR" "$module_dest" "$flake" "$host" >&2
    return 0
  fi

  case "$mode" in
    never)
      log "NixOS bridge state changed; run: sudo nixos-rebuild switch --impure --flake ${rebuild_ref}"
      ;;
    always)
      log "rebuilding NixOS to apply registry CA bridge"
      run_priv nixos-rebuild switch --impure --flake "$rebuild_ref"
      ;;
    prompt|*)
      if [[ "$HAS_TTY" != "1" ]]; then
        log "NixOS bridge state changed; run: sudo nixos-rebuild switch --impure --flake ${rebuild_ref}"
        return 0
      fi
      if prompt_yes_no "Run nixos-rebuild switch to apply registry CA bridge?" Y; then
        log "rebuilding NixOS to apply registry CA bridge"
        run_priv nixos-rebuild switch --impure --flake "$rebuild_ref"
      else
        log "NixOS bridge state changed; run later: sudo nixos-rebuild switch --impure --flake ${rebuild_ref}"
      fi
      ;;
  esac
}

install_backend_ca() {
  local cert_dir="$1"
  local cert_file="${cert_dir}/ca.crt"

  run_priv mkdir -p "$cert_dir"
  if [[ -n "$ca" ]]; then
    if [[ ! -s "$ca" ]]; then
      echo "CA file not found or empty: $ca" >&2
      exit 1
    fi
    run_priv cp "$ca" "$cert_file"
  fi
}

cert_dir="${CONTAINERD_CERTS_DIR_ROOT}/${host}"
run_priv mkdir -p "$cert_dir"

if [[ -n "$ca" ]]; then
  if [[ ! -s "$ca" ]]; then
    echo "CA file not found or empty: $ca" >&2
    exit 1
  fi
  run_priv cp "$ca" "$cert_dir/ca.crt"
fi

hosts_toml="$cert_dir/hosts.toml"
{
  echo "server = \"${scheme}://${host}\""
  echo ""
  echo "[host.\"${scheme}://${host}\"]"
  echo "  capabilities = [\"pull\", \"resolve\", \"push\"]"
  if [[ "$scheme" == "https" ]]; then
    if [[ $insecure -eq 1 ]]; then
      echo "  skip_verify = true"
    else
      echo "  ca = \"${cert_dir}/ca.crt\""
    fi
  fi
} | ${SUDO:+$SUDO }tee "$hosts_toml" >/dev/null

if [[ "$scheme" == "https" && $insecure -eq 0 && -n "$ca" ]]; then
  if [[ $podman_root -eq 1 ]]; then
    install_backend_ca "${PODMAN_CERTS_DIR_ROOT}/${host}"
  fi
  if [[ -n "$podman_user_home" ]]; then
    install_backend_ca "${podman_user_home%/}/.config/containers/certs.d/${host}"
  fi
  if [[ $docker_trust -eq 1 ]]; then
    install_backend_ca "${DOCKER_CERTS_DIR_ROOT}/${host}"
  fi
fi

if [[ $system_trust -eq 1 ]]; then
  trust_name="registry-${host//[:\/]/_}.crt"
  if k1s_is_nixos /etc/os-release; then
    sync_nixos_bridge_trust "$trust_name"
  else
    trust_dest="/usr/local/share/ca-certificates/${trust_name}"
    run_priv mkdir -p "$(dirname "$trust_dest")"
    run_priv cp "$ca" "$trust_dest"
    if command -v update-ca-certificates >/dev/null 2>&1; then
      run_priv update-ca-certificates >/dev/null 2>&1 || true
    elif command -v update-ca-trust >/dev/null 2>&1; then
      run_priv update-ca-trust extract >/dev/null 2>&1 || true
    fi
  fi
fi

config_changed=0
if ensure_containerd_config_version_v2; then
  config_changed=1
fi
if ensure_containerd_registry_config_path; then
  config_changed=1
fi
if ensure_containerd_runc_runtime; then
  config_changed=1
fi

if [[ $config_changed -eq 1 && $restart -eq 0 ]]; then
  restart=1
  echo "containerd restart required to apply config updates"
fi

if [[ $restart -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    run_priv systemctl restart containerd
  else
    echo "systemctl not found; restart containerd manually" >&2
  fi
fi

echo "containerd registry config written to ${hosts_toml}"
