#!/usr/bin/env bash
set -euo pipefail

env_file="state/bench-env/env.sh"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      env_file="$2"; shift 2;;
    -h|--help)
      echo "Usage: scripts/bench/bench_env_teardown.sh [--env env_file]";
      exit 0;;
    *)
      echo "[bench-env] unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -f "$env_file" ]]; then
  exit 0
fi

# shellcheck disable=SC1090
source "$env_file"

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

sudo_cmd=()
if [[ "${BENCH_CONTROLLER_SUDO:-0}" == "1" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  fi
fi

cri_use_sudo=0

process_alive() {
  local pid="$1"
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi
  if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
    "${sudo_cmd[@]}" kill -0 "$pid" >/dev/null 2>&1
    return $?
  fi
  return 1
}

kill_controller_pids() {
  local specs_desc="$1"
  shift
  local pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi
  echo "[bench-env] stopping controller(s) for specs=${specs_desc}: ${pids[*]}" >&2
  for pid in "${pids[@]}"; do
    if process_alive "$pid"; then
      kill "$pid" >/dev/null 2>&1 || "${sudo_cmd[@]}" kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  sleep 2
  for pid in "${pids[@]}"; do
    if process_alive "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || "${sudo_cmd[@]}" kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done
}

controller_matches_specs() {
  local cmd="$1"
  local specs_dir="$2"
  local rel_specs_dir="$3"
  if [[ -n "$specs_dir" && "$cmd" == *"--specs ${specs_dir}"* ]]; then
    return 0
  fi
  if [[ -n "$rel_specs_dir" && "$cmd" == *"--specs ${rel_specs_dir}"* ]]; then
    return 0
  fi
  return 1
}

collect_controller_pids_for_specs() {
  local specs_dir="$1"
  local rel_specs_dir="$2"
  local line pid cmd
  declare -A seen=()

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    pid="${line%% *}"
    cmd="${line#* }"
    [[ -z "$pid" || "$cmd" == "$line" ]] && continue
    if controller_matches_specs "$cmd" "$specs_dir" "$rel_specs_dir"; then
      if [[ -z "${seen[$pid]:-}" ]]; then
        printf '%s\n' "$pid"
        seen["$pid"]=1
      fi
    fi
  done < <(pgrep -af "python .*ae\\.controller" 2>/dev/null || true)

  if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      pid="${line%% *}"
      cmd="${line#* }"
      [[ -z "$pid" || "$cmd" == "$line" ]] && continue
      if controller_matches_specs "$cmd" "$specs_dir" "$rel_specs_dir"; then
        if [[ -z "${seen[$pid]:-}" ]]; then
          printf '%s\n' "$pid"
          seen["$pid"]=1
        fi
      fi
    done < <("${sudo_cmd[@]}" pgrep -af "python .*ae\\.controller" 2>/dev/null || true)
  fi
}

cri_can_nosudo() {
  command -v crictl >/dev/null 2>&1 && crictl --runtime-endpoint "$AE_CRI_ENDPOINT" info >/dev/null 2>&1
}

cri_switch_to_sudo() {
  local reason="${1:-runtime access}"
  if [[ "$cri_use_sudo" == "1" ]]; then
    return 0
  fi
  if [[ ${#sudo_cmd[@]} == 0 ]]; then
    return 1
  fi
  if ! "${sudo_cmd[@]}" true >/dev/null 2>&1; then
    return 1
  fi
  echo "[bench-env] non-sudo CRI access unavailable after ${reason}; falling back to sudo" >&2
  cri_use_sudo=1
  return 0
}

if [[ "${AE_RUNTIME_BACKEND:-}" == "cri" && -n "${AE_CRI_ENDPOINT:-}" ]] && command -v crictl >/dev/null 2>&1; then
  if ! cri_can_nosudo; then
    cri_use_sudo=1
  fi
fi

cri_cmd() {
  if [[ "$cri_use_sudo" == "1" ]]; then
    "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
    return $?
  fi
  crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
  local rc=$?
  if (( rc == 0 )); then
    return 0
  fi
  if ! cri_can_nosudo && cri_switch_to_sudo "crictl $1"; then
    "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
    return $?
  fi
  return $rc
}

cri_collect_app_state_json() {
  local app="$1"
  local pods_json="" containers_json="" query_failed=0
  if ! pods_json="$(cri_cmd pods -o json 2>/dev/null)"; then
    query_failed=1
  fi
  if ! containers_json="$(cri_cmd ps -a -o json 2>/dev/null)"; then
    query_failed=1
  fi
  PYTHONPATH="${PYTHONPATH:-}" "${PYTHON_BIN:-python}" - "$app" "$query_failed" <(printf '%s' "$pods_json") <(printf '%s' "$containers_json") <<'PY' || true
import json
import sys

app = sys.argv[1]
query_failed = bool(int(sys.argv[2]))
pods_path = sys.argv[3]
containers_path = sys.argv[4]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def labels_for(item):
    labels = item.get("labels") or {}
    if labels:
        return {str(k): str(v) for k, v in labels.items()}
    meta = item.get("metadata") or {}
    meta_labels = meta.get("labels") or {}
    return {str(k): str(v) for k, v in meta_labels.items()}


def item_name(item):
    meta = item.get("metadata") or {}
    return str(meta.get("name") or item.get("name") or "")


pods = load(pods_path)
containers = load(containers_path)
pod_ids = set()
live_pod_ids = set()

for pod in (pods.get("items") or pods.get("pods") or []):
    labels = labels_for(pod)
    name = item_name(pod)
    if labels.get("ae.app") == app or (name.startswith(f"{app}-rev") and not labels.get("ae.app")):
        pid = pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id")
        if pid:
            pod_ids.add(str(pid))
    pid = pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id")
    if pid:
        live_pod_ids.add(str(pid))

live_container_ids = set()
container_pod_ids = set()
orphan_container_ids = []
for container in (containers.get("containers") or containers.get("items") or []):
    labels = labels_for(container)
    replica_id = labels.get("ae.pod_name") or labels.get("ae.replica_id") or ""
    app_label = labels.get("ae.app") or ""
    if app_label != app and not replica_id.startswith(f"{app}-rev"):
        continue
    cid = container.get("id") or container.get("containerId") or container.get("container_id")
    if cid:
        live_container_ids.add(str(cid))
    pod_id = (
        container.get("podSandboxId")
        or container.get("pod_sandbox_id")
        or container.get("pod_id")
        or ""
    )
    if pod_id:
        pod_id = str(pod_id)
    if pod_id and pod_id in live_pod_ids:
        container_pod_ids.add(pod_id)
        continue
    if cid:
        orphan_container_ids.append(str(cid))

pod_ids.update(container_pod_ids)

print(
    json.dumps(
        {
            "query_failed": query_failed,
            "pod_ids": sorted(pod_ids),
            "live_container_ids": sorted(live_container_ids),
            "orphan_container_ids": sorted(set(orphan_container_ids)),
        }
    )
)
PY
}

cri_state_field_lines() {
  local field="$1"
  local state_json="$2"
  printf '%s' "$state_json" | "${PYTHON_BIN:-python}" -c '
import json, sys
field = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for item in data.get(field) or []:
    if item:
        print(str(item))
' "$field" || true
}

cri_state_query_failed() {
  local state_json="$1"
  printf '%s' "$state_json" | "${PYTHON_BIN:-python}" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if data.get("query_failed") else 1)
' >/dev/null 2>&1
}

cri_cleanup_app() {
  local app="$1"
  [[ -z "$app" ]] && return 0
  if [[ -z "${AE_CRI_ENDPOINT:-}" ]] || ! command -v crictl >/dev/null 2>&1; then
    return 0
  fi
  local timeout="${CRI_POD_CLEANUP_TIMEOUT:-30}"
  local settle="${CRI_POD_CLEANUP_SETTLE:-1}"
  local deadline=$((SECONDS + timeout))
  local state_json=""
  local -a pod_ids_arr=()
  local -a container_ids_arr=()
  local id pid cid cids

  while :; do
    state_json="$(cri_collect_app_state_json "$app")"
    if cri_state_query_failed "$state_json"; then
      if (( SECONDS >= deadline )); then
        echo "[bench-env] unable to inspect CRI state for app=${app} within ${timeout}s" >&2
        return 1
      fi
      sleep "$settle"
      continue
    fi
    pod_ids_arr=()
    container_ids_arr=()
    while IFS= read -r id; do
      [[ -n "$id" ]] && pod_ids_arr+=("$id")
    done < <(cri_state_field_lines "pod_ids" "$state_json")
    while IFS= read -r id; do
      [[ -n "$id" ]] && container_ids_arr+=("$id")
    done < <(cri_state_field_lines "orphan_container_ids" "$state_json")

    if (( ${#pod_ids_arr[@]} == 0 && ${#container_ids_arr[@]} == 0 )); then
      if (( settle > 0 )); then
        sleep "$settle"
      fi
      return 0
    fi

    local op_failed=0
    if (( ${#pod_ids_arr[@]} > 0 )); then
      echo "[bench-env] removing CRI pods for app=${app}: ${#pod_ids_arr[@]}" >&2
      for pid in "${pod_ids_arr[@]}"; do
        cids="$(cri_cmd ps -a --pod "$pid" -o json 2>/dev/null | "${PYTHON_BIN:-python}" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get("containers") or data.get("items") or []
print("\n".join([c.get("id","") for c in items if c.get("id")]))
' || true)"
        for cid in $cids; do
          if ! cri_cmd stop "$cid" >/dev/null 2>&1; then
            op_failed=1
          fi
          if ! cri_cmd rm "$cid" >/dev/null 2>&1; then
            op_failed=1
          fi
        done
        if ! cri_cmd stopp "$pid" >/dev/null 2>&1; then
          op_failed=1
        fi
        if ! cri_cmd rmp "$pid" >/dev/null 2>&1; then
          op_failed=1
        fi
      done

      state_json="$(cri_collect_app_state_json "$app")"
      if cri_state_query_failed "$state_json"; then
        op_failed=1
      fi
      container_ids_arr=()
      while IFS= read -r id; do
        [[ -n "$id" ]] && container_ids_arr+=("$id")
      done < <(cri_state_field_lines "orphan_container_ids" "$state_json")
    fi

    if (( ${#container_ids_arr[@]} > 0 )); then
      echo "[bench-env] removing orphan CRI containers for app=${app}: ${#container_ids_arr[@]}" >&2
      for cid in "${container_ids_arr[@]}"; do
        if ! cri_cmd stop "$cid" >/dev/null 2>&1; then
          op_failed=1
        fi
        if ! cri_cmd rm "$cid" >/dev/null 2>&1; then
          op_failed=1
        fi
      done
    fi

    state_json="$(cri_collect_app_state_json "$app")"
    if cri_state_query_failed "$state_json"; then
      if (( SECONDS >= deadline )); then
        echo "[bench-env] unable to inspect CRI state for app=${app} within ${timeout}s" >&2
        return 1
      fi
      sleep "$settle"
      continue
    fi
    pod_ids_arr=()
    container_ids_arr=()
    while IFS= read -r id; do
      [[ -n "$id" ]] && pod_ids_arr+=("$id")
    done < <(cri_state_field_lines "pod_ids" "$state_json")
    while IFS= read -r id; do
      [[ -n "$id" ]] && container_ids_arr+=("$id")
    done < <(cri_state_field_lines "orphan_container_ids" "$state_json")

    if (( ${#pod_ids_arr[@]} == 0 && ${#container_ids_arr[@]} == 0 )); then
      if (( settle > 0 )); then
        sleep "$settle"
      fi
      return 0
    fi

    if (( SECONDS >= deadline )); then
      if (( ${#pod_ids_arr[@]} > 0 )); then
        echo "[bench-env] remaining CRI pods for app=${app}: ${#pod_ids_arr[@]} (${pod_ids_arr[*]})" >&2
      fi
      if (( ${#container_ids_arr[@]} > 0 )); then
        echo "[bench-env] remaining orphan CRI containers for app=${app}: ${#container_ids_arr[@]} (${container_ids_arr[*]})" >&2
      fi
      return 1
    fi

    if (( op_failed > 0 )); then
      sleep "$settle"
    else
      sleep 1
    fi
  done
}

if [[ -n "${BENCH_CONTROLLER_PID:-}" ]]; then
  if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
    if "${sudo_cmd[@]}" kill -0 "$BENCH_CONTROLLER_PID" 2>/dev/null; then
      "${sudo_cmd[@]}" kill "$BENCH_CONTROLLER_PID" 2>/dev/null || true
      "${sudo_cmd[@]}" wait "$BENCH_CONTROLLER_PID" 2>/dev/null || true
    fi
  else
    if kill -0 "$BENCH_CONTROLLER_PID" 2>/dev/null; then
      kill "$BENCH_CONTROLLER_PID" 2>/dev/null || true
      wait "$BENCH_CONTROLLER_PID" 2>/dev/null || true
    fi
  fi
fi
if [[ -n "${BENCH_CONTROLLER_PID_FILE:-}" ]]; then
  if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
    "${sudo_cmd[@]}" rm -f "$BENCH_CONTROLLER_PID_FILE"
  else
    rm -f "$BENCH_CONTROLLER_PID_FILE"
  fi
fi

bench_spec_dir="${BENCH_SPEC_DIR:-${AE_SPECS_DIR:-}}"
bench_spec_rel=""
if [[ -n "$bench_spec_dir" && "$bench_spec_dir" == "$repo_root/"* ]]; then
  bench_spec_rel="${bench_spec_dir#"$repo_root"/}"
elif [[ "$bench_spec_dir" == state/* ]]; then
  bench_spec_rel="$bench_spec_dir"
fi

fallback_controller_pids=()
if [[ -n "$bench_spec_dir$bench_spec_rel" ]]; then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && fallback_controller_pids+=("$pid")
  done < <(collect_controller_pids_for_specs "$bench_spec_dir" "$bench_spec_rel")
fi
kill_controller_pids "${bench_spec_rel:-$bench_spec_dir}" "${fallback_controller_pids[@]}"

if [[ "${AE_RUNTIME_BACKEND:-}" == "cri" ]]; then
  cri_cleanup_app "${BENCH_PRIMARY_APP:-}"
fi

if [[ "${BENCH_KEEP_ENV:-0}" != "1" ]]; then
  if [[ -n "${BENCH_ENV_DIR:-}" && -d "$BENCH_ENV_DIR" ]]; then
    if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
      "${sudo_cmd[@]}" rm -rf "$BENCH_ENV_DIR"
    else
      rm -rf "$BENCH_ENV_DIR"
    fi
  fi
  if [[ ${#sudo_cmd[@]} -gt 0 ]]; then
    "${sudo_cmd[@]}" rm -f "$env_file"
  else
    rm -f "$env_file"
  fi
else
  echo "[bench-env] keeping env dir $BENCH_ENV_DIR (BENCH_KEEP_ENV=1)" >&2
  echo "[bench-env] keeping env file $env_file" >&2
fi
