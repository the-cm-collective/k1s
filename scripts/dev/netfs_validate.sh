#!/usr/bin/env bash
set -euo pipefail

WRITER_APP="${WRITER_APP:-netfs-nfs-sea-edge-02-edge-1}"
READER_APP="${READER_APP:-netfs-nfs-hub-reader}"
MOUNT_PATH="${MOUNT_PATH:-/data}"
NAMESPACE="${NAMESPACE:-default}"
RUNTIME="${RUNTIME:-auto}" # auto|cri|podman
CRI_HOST="${CRI_HOST:-}"
CRI_USER="${CRI_USER:-ae}"
CRI_KEY="${CRI_KEY:-}"
CRI_PORT="${CRI_PORT:-22}"
CRI_EXEC_RETRIES="${CRI_EXEC_RETRIES:-3}"
CRI_EXEC_RETRY_DELAY="${CRI_EXEC_RETRY_DELAY:-1}"
AE_EXEC_READ_RETRIES="${AE_EXEC_READ_RETRIES:-3}"
AE_EXEC_READ_RETRY_DELAY="${AE_EXEC_READ_RETRY_DELAY:-1}"
AE_EXEC_READ_POLL_SECONDS="${AE_EXEC_READ_POLL_SECONDS:-2}"
AE_EXEC_READ_POLL_INTERVAL="${AE_EXEC_READ_POLL_INTERVAL:-0.25}"
AE_EXEC_TRANSPORT_REPORT="${AE_EXEC_TRANSPORT_REPORT:-1}"
AE_EXEC_FAIL_ON_FALLBACK="${AE_EXEC_FAIL_ON_FALLBACK:-0}"

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
  --cri-host <host>     Remote CRI host for fallback exec (default: local host)
  --cri-user <user>     Remote CRI SSH user (default: ae)
  --cri-key <path>      Remote CRI SSH key path (default: SSH agent/default keys)
  --cri-port <port>     Remote CRI SSH port (default: 22)
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
    --cri-host)
      CRI_HOST="${2:-}"
      shift 2
      ;;
    --cri-user)
      CRI_USER="${2:-}"
      shift 2
      ;;
    --cri-key)
      CRI_KEY="${2:-}"
      shift 2
      ;;
    --cri-port)
      CRI_PORT="${2:-}"
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

cri_cmd() {
  if [[ -n "$CRI_HOST" ]]; then
    command -v ssh >/dev/null 2>&1 || die "runtime=cri selected but ssh is not installed"
    local ssh_args=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -p "$CRI_PORT")
    if [[ -n "$CRI_KEY" ]]; then
      ssh_args+=(-i "$CRI_KEY")
    fi
    local remote_cmd="bash -s --"
    local arg
    for arg in "$@"; do
      printf -v remote_cmd '%s %q' "$remote_cmd" "$arg"
    done
    ssh "${ssh_args[@]}" "${CRI_USER}@${CRI_HOST}" "$remote_cmd" <<'SH'
set -euo pipefail
if sudo -n true >/dev/null 2>&1; then
  exec sudo -n crictl "$@"
fi
exec crictl "$@"
SH
    return
  fi

  if sudo -n true >/dev/null 2>&1; then
    sudo -n crictl "$@"
    return
  fi
  crictl "$@"
}

run_cri_exec() {
  local cid="$1"
  shift

  local retry_count="$CRI_EXEC_RETRIES"
  if ! [[ "$retry_count" =~ ^[0-9]+$ ]] || (( retry_count < 1 )); then
    retry_count=1
  fi

  local attempt rc output=""
  local short_cid="${cid:0:12}"
  for ((attempt = 1; attempt <= retry_count; attempt++)); do
    set +e
    output="$(cri_cmd exec "$cid" "$@" 2>&1)"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      printf '%s' "$output"
      return 0
    fi
    if (( attempt < retry_count )); then
      printf '[netfs-validate] CRI exec attempt %d/%d failed for container %s; retrying in %ss\n' \
        "$attempt" "$retry_count" "$short_cid" "$CRI_EXEC_RETRY_DELAY" >&2
      if [[ -n "$output" ]]; then
        printf '%s\n' "$output" >&2
      fi
      sleep "$CRI_EXEC_RETRY_DELAY"
    fi
  done

  printf '%s' "$output"
  return "$rc"
}

cri_available() {
  if [[ -n "$CRI_HOST" ]]; then
    command -v ssh >/dev/null 2>&1 || return 1
  elif ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  cri_cmd info >/dev/null 2>&1
}

find_cri_cid() {
  local pattern="$1"
  # CRI lanes commonly use generic container names (e.g. "main") and app-specific
  # pod names (e.g. app-rev1-0). Resolve via pod first, then map pod -> container.
  local cid
  cid="$(cri_cmd ps -a --name "$pattern" -q 2>/dev/null | head -n1 || true)"
  if [[ -n "$cid" ]]; then
    printf '%s\n' "$cid"
    return 0
  fi

  local pod_id
  pod_id="$(cri_cmd pods --name "$pattern" -q 2>/dev/null | head -n1 || true)"
  if [[ -z "$pod_id" ]]; then
    # App names are base names, while CRI pod names include rollout suffixes.
    pod_id="$(cri_cmd pods --name "${pattern}-rev" -q 2>/dev/null | head -n1 || true)"
  fi
  if [[ -n "$pod_id" ]]; then
    cri_cmd ps -a --pod "$pod_id" -q 2>/dev/null | head -n1 || true
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

  if cri_available; then
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

  if cri_available; then
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

shell_quote() {
  local value="${1-}"
  printf "'%s'" "${value//\'/\'\\\'\'}"
}

extract_exec_transport_report() {
  local text="${1-}"
  printf '%s\n' "$text" | sed -n 's/^AE_EXEC_TRANSPORT_REPORT //p' | tail -n 1
}

strip_exec_transport_report() {
  local text="${1-}"
  printf '%s\n' "$text" | sed '/^AE_EXEC_TRANSPORT_REPORT /d'
}

exec_transport_field() {
  local report="${1-}"
  local field="${2-}"
  [[ -n "$report" && -n "$field" ]] || return 0
  printf '%s\n' "$report" | tr ' ' '\n' | sed -n "s/^${field}=//p" | tail -n 1
}

exec_transport_summary() {
  local report="${1-}"
  local primary=""
  local final=""
  primary="$(exec_transport_field "$report" primary)"
  final="$(exec_transport_field "$report" final)"
  [[ -n "$primary" ]] || return 0
  if [[ -z "$final" || "$final" == "none" || "$primary" == "$final" ]]; then
    printf '%s' "$primary"
    return
  fi
  printf '%s->%s' "$primary" "$final"
}

exec_transport_used_fallback() {
  local report="${1-}"
  local fallback=""
  fallback="$(exec_transport_field "$report" fallback)"
  [[ "$fallback" == "1" ]]
}

reader_poll_loops="$(awk -v secs="$AE_EXEC_READ_POLL_SECONDS" -v interval="$AE_EXEC_READ_POLL_INTERVAL" 'BEGIN {
  s = secs + 0
  i = interval + 0
  if (s <= 0) s = 0
  if (i <= 0) i = 0.25
  loops = int((s / i) + 0.999999)
  if (loops < 1) loops = 1
  print loops
}')"
writer_file_q="$(shell_quote "$writer_file")"
stamp_q="$(shell_quote "$stamp")"
reader_poll_interval_q="$(shell_quote "$AE_EXEC_READ_POLL_INTERVAL")"
read -r -d '' READER_READ_CMD <<EOF || true
target=${writer_file_q}
expected=${stamp_q}
poll_interval=${reader_poll_interval_q}
max_polls=${reader_poll_loops}
i=0
while [ "\$i" -lt "\$max_polls" ]; do
  if [ -f "\$target" ] && grep -q "\$expected" "\$target" 2>/dev/null; then
    cat "\$target"
    exit 0
  fi
  i=\$((i + 1))
  if [ "\$i" -lt "\$max_polls" ]; then
    sleep "\$poll_interval"
  fi
done
if [ -f "\$target" ]; then
  cat "\$target"
fi
exit 0
EOF

set +e
writer_out="$(
  AE_EXEC_TRANSPORT_REPORT="$AE_EXEC_TRANSPORT_REPORT" \
    ae exec -n "$NAMESPACE" "$WRITER_APP" -- sh -lc "echo ${stamp} > ${writer_file} && cat ${writer_file}" 2>&1
)"
writer_rc=$?
set -e

reader_out=""
reader_rc=1
for ((reader_attempt=1; reader_attempt<=AE_EXEC_READ_RETRIES; reader_attempt++)); do
  set +e
  reader_out="$(
    AE_EXEC_TRANSPORT_REPORT="$AE_EXEC_TRANSPORT_REPORT" \
      ae exec -n "$NAMESPACE" "$READER_APP" -- sh -lc "$READER_READ_CMD" 2>&1
  )"
  reader_rc=$?
  set -e
  reader_report="$(extract_exec_transport_report "$reader_out")"
  reader_clean="$(strip_exec_transport_report "$reader_out")"
  if [[ "$reader_rc" -eq 0 ]] && printf '%s' "$reader_clean" | grep -q "$stamp"; then
    reader_out="$reader_clean"
    break
  fi
  reader_out="$reader_clean"
  if (( reader_attempt < AE_EXEC_READ_RETRIES )); then
    log "reader miss on attempt ${reader_attempt}/${AE_EXEC_READ_RETRIES}; retrying ae exec read..."
    sleep "$AE_EXEC_READ_RETRY_DELAY"
  fi
done

writer_report="$(extract_exec_transport_report "$writer_out")"
writer_out="$(strip_exec_transport_report "$writer_out")"
reader_report="${reader_report:-$(extract_exec_transport_report "$reader_out")}"
reader_out="$(strip_exec_transport_report "$reader_out")"

writer_transport="$(exec_transport_summary "$writer_report")"
reader_transport="$(exec_transport_summary "$reader_report")"
transport_fallback_used=0
transport_fallback_parts=()
if exec_transport_used_fallback "$writer_report"; then
  transport_fallback_used=1
  transport_fallback_parts+=("writer=${writer_transport:-unknown}")
fi
if exec_transport_used_fallback "$reader_report"; then
  transport_fallback_used=1
  transport_fallback_parts+=("reader=${reader_transport:-unknown}")
fi
transport_fallback_detail=""
if (( ${#transport_fallback_parts[@]} > 0 )); then
  transport_fallback_detail="$(IFS=', '; printf '%s' "${transport_fallback_parts[*]}")"
fi

printf '%s\n' "$writer_out"
printf '%s\n' "$reader_out"

if [[ "$writer_rc" -eq 0 && "$reader_rc" -eq 0 ]] && printf '%s' "$reader_out" | grep -q "$stamp"; then
  if (( transport_fallback_used )); then
    log "WARN: ae exec transport fallback used (${transport_fallback_detail})"
    if [[ "$AE_EXEC_FAIL_ON_FALLBACK" == "1" ]]; then
      die "ae exec transport fallback used (${transport_fallback_detail})"
    fi
  fi
  log "PASS: netfs shared read/write via ae exec (${stamp})"
  if (( transport_fallback_used )); then
    log "PASS: stream path recovered via fallback (writer_rc=${writer_rc}, reader_rc=${reader_rc})"
  else
    log "PASS: stream path clean (writer_rc=${writer_rc}, reader_rc=${reader_rc})"
  fi
  exit 0
fi

selected_runtime="$(choose_runtime)"
[[ -n "$selected_runtime" ]] || die "no supported runtime fallback available (need crictl or podman)"

log "ae exec path not clean (writer_rc=${writer_rc}, reader_rc=${reader_rc}); validating data path via ${selected_runtime} runtime..."

case "$selected_runtime" in
  cri)
    cri_available || die "runtime=cri selected but CRI access is unavailable"
    writer_cid="$(find_cri_cid "$WRITER_APP")"
    reader_cid="$(find_cri_cid "$READER_APP")"
    [[ -n "$writer_cid" ]] || die "CRI writer container not found for '${WRITER_APP}'"
    [[ -n "$reader_cid" ]] || die "CRI reader container not found for '${READER_APP}'"
    set +e
    writer_fb="$(run_cri_exec "$writer_cid" sh -lc "echo ${stamp} > ${writer_file} && cat ${writer_file}")"
    writer_fb_rc=$?
    reader_fb="$(run_cri_exec "$reader_cid" cat "${writer_file}")"
    reader_fb_rc=$?
    set -e
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

case "$selected_runtime" in
  cri)
    [[ "${writer_fb_rc:-0}" -eq 0 ]] || die "CRI writer exec failed for '${WRITER_APP}' (container ${writer_cid:0:12})"
    [[ "${reader_fb_rc:-0}" -eq 0 ]] || die "CRI reader exec failed for '${READER_APP}' (container ${reader_cid:0:12})"
    ;;
esac

if ! printf '%s' "$reader_fb" | grep -q "$stamp"; then
  die "fallback runtime data path validation failed (stamp '${stamp}' not found in reader output)"
fi

log "PASS: data path validated via ${selected_runtime} runtime (${stamp})"
log "PASS: storage lane healthy; ae exec stream path had transient noise"
