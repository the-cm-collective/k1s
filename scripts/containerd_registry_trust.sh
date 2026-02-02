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

if [[ $restart -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl restart containerd
  else
    echo "systemctl not found; restart containerd manually" >&2
  fi
fi

echo "containerd registry config written to ${hosts_toml}"
