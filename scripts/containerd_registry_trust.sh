#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/containerd_registry_trust.sh [options]

Options:
  --host <host:port>   Registry host (required)
  --ca <path>          CA cert path (required for https unless --insecure)
  --scheme <http|https>  Scheme (default: https)
  --insecure           Skip TLS verification (writes skip_verify = true)
  --system-trust       Install CA into system trust store (Linux)
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
system_trust=0
restart=0

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

SUDO=""
if [[ "$EUID" -ne 0 ]]; then
  SUDO="sudo"
fi

ensure_containerd_config_version_v2() {
  local config_file="/etc/containerd/config.toml"
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
  local config_file="/etc/containerd/config.toml"
  local desired="/etc/containerd/certs.d"

  if [[ ! -f "$config_file" ]]; then
    return 1
  fi

  local tmp
  tmp="$(mktemp)"
  awk -v desired="$desired" '
    BEGIN { in_table=0; done=0 }
    {
      if ($0 ~ /^\[plugins\."io\.containerd\.grpc\.v1\.cri"\.registry\][[:space:]]*$/) {
        print
        in_table=1
        next
      }
      if (in_table && $0 ~ /^\[/) {
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
  local config_file="/etc/containerd/config.toml"
  [[ -f "$config_file" ]] || return 1

  if grep -Eq '^\[plugins\."io\.containerd\.grpc\.v1\.cri"\.containerd\.runtimes\.runc\][[:space:]]*$' "$config_file"; then
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

cert_dir="/etc/containerd/certs.d/${host}"
$SUDO mkdir -p "$cert_dir"

if [[ -n "$ca" ]]; then
  if [[ ! -s "$ca" ]]; then
    echo "CA file not found or empty: $ca" >&2
    exit 1
  fi
  $SUDO cp "$ca" "$cert_dir/ca.crt"
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
} | $SUDO tee "$hosts_toml" >/dev/null

if [[ $system_trust -eq 1 ]]; then
  trust_name="registry-${host//[:\/]/_}.crt"
  trust_dest="/usr/local/share/ca-certificates/${trust_name}"
  $SUDO cp "$ca" "$trust_dest"
  if command -v update-ca-certificates >/dev/null 2>&1; then
    $SUDO update-ca-certificates >/dev/null 2>&1 || true
  elif command -v update-ca-trust >/dev/null 2>&1; then
    $SUDO update-ca-trust extract >/dev/null 2>&1 || true
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
    $SUDO systemctl restart containerd
  else
    echo "systemctl not found; restart containerd manually" >&2
  fi
fi

echo "containerd registry config written to ${hosts_toml}"
