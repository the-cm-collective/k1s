#!/usr/bin/env bash
set -euo pipefail

MODE=${1:-pre}
case "$MODE" in
  pre|post)
    ;;
  -h|--help)
    cat <<'USAGE'
Usage: scripts/bench/k1nd_sanitize.sh [pre|post]

pre  - Tear down prior labs-aio stack (unless K1ND_SKIP_PRE_DOWN=1) and remove
       stray ae.app containers across Docker/Podman before running k1nd benches.
post - Remove any ae.app containers left by the run and optionally tear down the
       labs-aio stack (set K1ND_SKIP_POST_DOWN=1 to keep it running).
USAGE
    exit 0
    ;;
  *)
    echo "[k1nd-clean] unsupported mode: $MODE" >&2
    exit 2
    ;;
esac

COMPOSE_FILE=${K1ND_COMPOSE_FILE:-ops/dev/labs-aio.yaml}
COMPOSE_ARGS=(docker compose -f "$COMPOSE_FILE")

log() {
  echo "[k1nd-clean] $*" >&2
}

stack_down() {
  if ! command -v docker >/dev/null 2>&1; then
    return
  fi
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    return
  fi
  log "bringing down labs stack: $COMPOSE_FILE"
  "${COMPOSE_ARGS[@]}" down --remove-orphans >/dev/null 2>&1 || true
}

cleanup_engine() {
  local engine="$1"
  command -v "$engine" >/dev/null 2>&1 || return
  local combined
  combined=$( {
    "$engine" ps -aq --filter label=ae.app 2>/dev/null || true
    "$engine" ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$combined" ]]; then
    log "removing $engine containers: $combined"
    # shellcheck disable=SC2086
    "$engine" rm -f $combined >/dev/null 2>&1 || true
  fi
}

case "$MODE" in
  pre)
    if [[ "${K1ND_SKIP_PRE_DOWN:-0}" != "1" ]]; then
      stack_down
    else
      log "skipping pre-run stack teardown (K1ND_SKIP_PRE_DOWN=1)"
    fi
    cleanup_engine docker
    cleanup_engine podman
    ;;
  post)
    cleanup_engine docker
    cleanup_engine podman
    if [[ "${K1ND_SKIP_POST_DOWN:-0}" != "1" ]]; then
      stack_down
    else
      log "skipping post-run stack teardown (K1ND_SKIP_POST_DOWN=1)"
    fi
    ;;
esac
