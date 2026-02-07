#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/bench/clean_state.sh [--bench|--dev] [--confirm] [--keep-tls]

  --bench    Remove benchmark-only state directories (state/bench-*)
  --dev      Wipe the full state/ directory (requires --confirm)
  --confirm   Required for --dev (or set CONFIRM=1)
  --keep-tls  Preserve TLS/SSL artifacts when wiping dev state

Examples:
  scripts/bench/clean_state.sh --bench
  CONFIRM=1 scripts/bench/clean_state.sh --dev
USAGE
}

mode=""
confirm="${CONFIRM:-0}"
keep_tls="${KEEP_TLS:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench) mode="bench"; shift;;
    --dev) mode="dev"; shift;;
    --confirm) confirm=1; shift;;
    --keep-tls) keep_tls=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "[clean-state] unknown arg: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$mode" ]]; then
  echo "[clean-state] choose --bench or --dev" >&2
  usage
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
state_dir="$repo_root/state"

if [[ "$mode" == "bench" ]]; then
  if [[ ! -d "$state_dir" ]]; then
    echo "[clean-state] state directory not found: $state_dir" >&2
    exit 0
  fi
  shopt -s nullglob
  bench_dirs=("$state_dir"/bench-*)
  shopt -u nullglob
  if (( ${#bench_dirs[@]} == 0 )); then
    echo "[clean-state] no bench state directories to remove" >&2
    exit 0
  fi
  rm -rf "${bench_dirs[@]}" 2>/dev/null || true
  echo "[clean-state] removed: ${bench_dirs[*]}" >&2
  exit 0
fi

# --dev: wipe entire state directory (optionally preserve TLS/SSL artifacts)
if [[ "$confirm" != "1" ]]; then
  echo "[clean-state] refusing to delete $state_dir without --confirm (or CONFIRM=1)" >&2
  exit 2
fi

if [[ ! -d "$state_dir" ]]; then
  echo "[clean-state] state directory not found: $state_dir" >&2
  exit 0
fi

if [[ "$keep_tls" == "1" ]]; then
  tmp_dir="$(mktemp -d)"
  preserve() {
    local rel="$1"
    local src="$state_dir/$rel"
    local dst="$tmp_dir/$rel"
    if [[ -e "$src" ]]; then
      mkdir -p "$(dirname "$dst")"
      cp -a "$src" "$dst" 2>/dev/null || true
    fi
  }
  preserve "certs"
  preserve "caddy-data"
  preserve "tls"
  if [[ -d "$state_dir/profiles" ]]; then
    while IFS= read -r f; do
      rel="${f#"$state_dir/"}"
      preserve "$rel"
    done < <(find "$state_dir/profiles" -type f \( -name 'apishim.crt' -o -name 'apishim.key' \) 2>/dev/null || true)
  fi
fi

if rm -rf "$state_dir" 2>/dev/null; then
  echo "[clean-state] removed $state_dir" >&2
else
  echo "[clean-state] failed to remove $state_dir (permissions?). Try: sudo rm -rf $state_dir" >&2
  exit 1
fi

if [[ "$keep_tls" == "1" ]]; then
  mkdir -p "$state_dir"
  if [[ -d "$tmp_dir" ]]; then
    (cd "$tmp_dir" && find . -mindepth 1 -maxdepth 1 -exec cp -a {} "$state_dir"/ \;) >/dev/null 2>&1 || true
    rm -rf "$tmp_dir" 2>/dev/null || true
  fi
  echo "[clean-state] preserved TLS/SSL artifacts" >&2
fi
