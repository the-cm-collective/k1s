#!/usr/bin/env bash
set -euo pipefail

# Ensure rootful Podman socket is available for sudo/rootful runs.
# - Prefer systemd socket activation when available.
# - Fallback to a background podman system service.

if ! command -v podman >/dev/null 2>&1; then
  echo "[rootful-socket] podman not found; skipping" >&2
  exit 0
fi

root_sock="${PODMAN_ROOTFUL_SOCKET:-/run/podman/podman.sock}"

sudo_cmd=()
if [[ $(id -u) -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[rootful-socket] sudo not found; cannot start rootful podman socket" >&2
    exit 1
  fi
  sudo_cmd=(sudo)
fi

socket_ready() {
  "${sudo_cmd[@]}" test -S "$root_sock" >/dev/null 2>&1
}

if socket_ready; then
  exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
  "${sudo_cmd[@]}" systemctl start podman.socket >/dev/null 2>&1 || true
  sleep 1
  if socket_ready; then
    exit 0
  fi
fi

# Fallback: run a persistent service to create the socket.
${sudo_cmd[@]} nohup podman system service -t 0 >/dev/null 2>&1 &
sleep 2
if socket_ready; then
  exit 0
fi

echo "[rootful-socket] rootful podman socket not available (expected ${root_sock})" >&2
exit 1
