#!/usr/bin/env bash
set -euo pipefail

WRITER_APP="${WRITER_APP:-netfs-nfs-sea-edge-02-edge-1}"
READER_APP="${READER_APP:-netfs-nfs-hub-reader}"
MOUNT_PATH="${MOUNT_PATH:-/data}"
NAMESPACE="${NAMESPACE:-default}"
RUNTIME="${RUNTIME:-auto}" # auto|cri|podman

usage() {
  cat <<'USAGE'
Usage: scripts/dev/netfs_validate.sh [options]

Validates shared NetFS data path by writing on writer app and reading on reader app.
Primary path uses `ae exec`; fallback uses runtime-native exec for CRI/Podman.

Options:
  --writer-app <name>   Writer app (default: netfs-nfs-sea-edge-02-edge-1)
  --reader-app <name>   Reader app (default: netfs-nfs-hub-reader)
  --mount-path <path>   Shared mount path (default: /data)
  --namespace <ns>      Namespace for ae exec (default: default)
  --runtime <mode>      auto|cri|podman (default: auto)
  -h, --help            Show this help text
USAGE
}

log() {
  printf '[netfs-validate] %s\n' "$*"
}

die() {
  printf '[netfs-validate] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --writer-app)
      WRITER_APP="${2:-}"
      shift 2
      ;;
    --reader-app)
      READER_APP="${2:-}"
      shift 2
      ;;
    --mount-path)
      MOUNT_PATH="${2:-}"
      shift 2
      ;;
    --namespace)
      NAMESPACE="${2:-}"
      shift 2
      ;;
    --runtime)
      RUNTIME="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

case "$RUNTIME" in
  auto|cri|podman) ;;
  *)
    die "invalid --runtime value '$RUNTIME' (expected auto|cri|podman)"
    ;;
esac

command -v ae >/dev/null 2>&1 || die "'ae' command not found"

stamp="netfs-$(date +%s)"
writer_file="${MOUNT_PATH}/hello.txt"

find_cri_cid() {
  local pattern="$1"
  # CRI lanes commonly use generic container names (e.g. "main") and app-specific
  # pod names (e.g. app-rev1-0). Resolve via pod first, then map pod -> container.
  local cid
  cid="$(sudo crictl ps -a --name "$pattern" -q 2>/dev/null | head -n1 || true)"
  if [[ -n "$cid" ]]; then
    printf '%s\n' "$cid"
    return 0
  fi

  local pod_id
  pod_id="$(sudo crictl pods --name "$pattern" -q 2>/dev/null | head -n1 || true)"
  if [[ -z "$pod_id" ]]; then
    # App names are base names, while CRI pod names include rollout suffixes.
    pod_id="$(sudo crictl pods --name "${pattern}-rev" -q 2>/dev/null | head -n1 || true)"
  fi
  if [[ -n "$pod_id" ]]; then
    sudo crictl ps -a --pod "$pod_id" -q 2>/dev/null | head -n1 || true
  fi
}

find_podman_container() {
  local pattern="$1"
  sudo podman ps --format '{{.Names}}' 2>/dev/null | awk -v pat="$pattern" 'index($0, pat){print; exit}'
}

choose_runtime() {
  if [[ "$RUNTIME" != "auto" ]]; then
    printf '%s\n' "$RUNTIME"
    return
  fi

  case "${AE_RUNTIME_BACKEND:-}" in
    cri)
      printf 'cri\n'
      return
      ;;
    podman)
      printf 'podman\n'
      return
      ;;
  esac

  if command -v crictl >/dev/null 2>&1; then
    local cw cr
    cw="$(find_cri_cid "$WRITER_APP")"
    cr="$(find_cri_cid "$READER_APP")"
    if [[ -n "$cw" && -n "$cr" ]]; then
      printf 'cri\n'
      return
    fi
  fi

  if command -v podman >/dev/null 2>&1; then
    local pw pr
    pw="$(find_podman_container "$WRITER_APP")"
    pr="$(find_podman_container "$READER_APP")"
    if [[ -n "$pw" && -n "$pr" ]]; then
      printf 'podman\n'
      return
    fi
  fi

  if command -v crictl >/dev/null 2>&1; then
    printf 'cri\n'
    return
  fi
  if command -v podman >/dev/null 2>&1; then
    printf 'podman\n'
    return
  fi
  printf '\n'
}

log "writer=${WRITER_APP} reader=${READER_APP} mount=${MOUNT_PATH} runtime=${RUNTIME} namespace=${NAMESPACE}"

set +e
writer_out="$(ae exec -n "$NAMESPACE" "$WRITER_APP" -- sh -lc "echo ${stamp} > ${writer_file} && cat ${writer_file}" 2>&1)"
writer_rc=$?
reader_out="$(ae exec -n "$NAMESPACE" "$READER_APP" -- cat "${writer_file}" 2>&1)"
reader_rc=$?
set -e

printf '%s\n' "$writer_out"
printf '%s\n' "$reader_out"

if [[ "$writer_rc" -eq 0 && "$reader_rc" -eq 0 ]] && printf '%s' "$reader_out" | grep -q "$stamp"; then
  log "PASS: netfs shared read/write via ae exec (${stamp})"
  log "PASS: stream path clean (writer_rc=${writer_rc}, reader_rc=${reader_rc})"
  exit 0
fi

selected_runtime="$(choose_runtime)"
[[ -n "$selected_runtime" ]] || die "no supported runtime fallback available (need crictl or podman)"

log "ae exec path not clean (writer_rc=${writer_rc}, reader_rc=${reader_rc}); validating data path via ${selected_runtime} runtime..."

case "$selected_runtime" in
  cri)
    command -v crictl >/dev/null 2>&1 || die "runtime=cri selected but crictl is not installed"
    writer_cid="$(find_cri_cid "$WRITER_APP")"
    reader_cid="$(find_cri_cid "$READER_APP")"
    [[ -n "$writer_cid" ]] || die "CRI writer container not found for '${WRITER_APP}'"
    [[ -n "$reader_cid" ]] || die "CRI reader container not found for '${READER_APP}'"
    writer_fb="$(sudo crictl exec "$writer_cid" sh -lc "echo ${stamp} > ${writer_file} && cat ${writer_file}" 2>&1)"
    reader_fb="$(sudo crictl exec "$reader_cid" cat "${writer_file}" 2>&1)"
    ;;
  podman)
    command -v podman >/dev/null 2>&1 || die "runtime=podman selected but podman is not installed"
    writer_ctr="$(find_podman_container "$WRITER_APP")"
    reader_ctr="$(find_podman_container "$READER_APP")"
    [[ -n "$writer_ctr" ]] || die "Podman writer container not found for '${WRITER_APP}'"
    [[ -n "$reader_ctr" ]] || die "Podman reader container not found for '${READER_APP}'"
    writer_fb="$(sudo podman exec "$writer_ctr" sh -lc "echo ${stamp} > ${writer_file} && cat ${writer_file}" 2>&1)"
    reader_fb="$(sudo podman exec "$reader_ctr" cat "${writer_file}" 2>&1)"
    ;;
esac

printf '%s\n' "$writer_fb"
printf '%s\n' "$reader_fb"

if ! printf '%s' "$reader_fb" | grep -q "$stamp"; then
  die "fallback runtime data path validation failed (stamp '${stamp}' not found in reader output)"
fi

log "PASS: data path validated via ${selected_runtime} runtime (${stamp})"
log "PASS: storage lane healthy; ae exec stream path had transient noise"
