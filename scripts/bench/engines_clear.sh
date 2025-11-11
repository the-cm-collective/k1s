#!/usr/bin/env bash
set -euo pipefail

# Clear host container engines before benchmarks to avoid contamination.
#
# - Stops and removes ALL Docker and Podman (rootful) containers.
# - Intended to be run with sudo to cover rootful engines used by k3d/k3s.
# - Rootless Podman (per-user) is NOT affected when running with sudo.
#
# Usage:
#   sudo ./scripts/bench/engines_clear.sh --confirm
#   # or via Makefile:
#   sudo make bench-engines-clear CONFIRM=1

confirm=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm) confirm=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

warn() { echo "[engines-clear] $*" >&2; }

summary_line() {
  local which=$1
  local count=$2
  if (( count == 0 )); then
    warn "$which: clear (0 containers)"
  else
    warn "$which: $count container(s) present"
  fi
}

clear_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker not found; skipping"
    return 0
  fi
  local ids; ids=$(docker ps -aq 2>/dev/null || true)
  local cnt=0
  [[ -n "$ids" ]] && cnt=$(echo "$ids" | wc -l | tr -d ' \t')
  summary_line "docker" "$cnt"
  if (( cnt > 0 )); then
    if (( confirm == 1 )); then
      warn "docker: removing $cnt container(s)"
      docker rm -fv $ids >/dev/null 2>&1 || true
    else
      warn "docker: pass --confirm (or CONFIRM=1) to remove containers"
    fi
  fi
}

clear_podman() {
  if ! command -v podman >/dev/null 2>&1; then
    warn "podman not found; skipping"
    return 0
  fi
  # Rootful Podman (since we expect sudo)
  local ids; ids=$(podman ps -aq 2>/dev/null || true)
  local cnt=0
  [[ -n "$ids" ]] && cnt=$(echo "$ids" | wc -l | tr -d ' \t')
  summary_line "podman" "$cnt"
  if (( cnt > 0 )); then
    if (( confirm == 1 )); then
      warn "podman: removing $cnt container(s)"
      podman rm -fv $ids >/dev/null 2>&1 || true
      # Also remove any leftover pods to keep ps -a quiet
      podman pod rm -fa >/dev/null 2>&1 || true
    else
      warn "podman: pass --confirm (or CONFIRM=1) to remove containers"
    fi
  fi
}

warn "starting (confirm=${confirm})"
clear_docker
clear_podman

# Verify state
rc=0
if command -v docker >/dev/null 2>&1; then
  dcnt=$(docker ps -aq 2>/dev/null | wc -l | tr -d ' \t' || echo 0)
  if (( dcnt > 0 )); then warn "docker: still has $dcnt container(s)"; rc=1; fi
fi
if command -v podman >/dev/null 2>&1; then
  pcnt=$(podman ps -aq 2>/dev/null | wc -l | tr -d ' \t' || echo 0)
  if (( pcnt > 0 )); then warn "podman: still has $pcnt container(s)"; rc=1; fi
fi

if (( rc == 0 )); then
  warn "engines clear: OK"
else
  warn "engines clear: NOT FULLY CLEAN (see above). You may need to stop services or check rootless containers."
fi

exit $rc

