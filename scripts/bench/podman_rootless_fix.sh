#!/usr/bin/env bash
set -euo pipefail

# Attempt to fix common rootless Podman issues before rootless benchmarks.
# - Ensure lingering is enabled so /run/user/$UID exists for crun.
# - Restart the user-level podman service/socket to clear stale state.
# - Reset crun session dirs.

uid=$(id -u)
if [[ $uid -eq 0 ]]; then
  echo "[rootless-fix] skipping (running as root)" >&2
  exit 0
fi

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
fi

if systemctl --user status podman.socket >/dev/null 2>&1; then
  systemctl --user stop podman.socket >/dev/null 2>&1 || true
  systemctl --user stop podman.service >/dev/null 2>&1 || true
  systemctl --user start podman.socket >/dev/null 2>&1 || true
else
  podman system service -t 0 >/dev/null 2>&1 &
fi

# Clean up stale crun dirs under /run/user/$UID/crun
if [[ -d /run/user/$uid/crun ]]; then
  find "/run/user/$uid/crun" -mindepth 1 -maxdepth 1 -type d -mmin +1 -exec rm -rf {} + 2>/dev/null || true
fi

exit 0
