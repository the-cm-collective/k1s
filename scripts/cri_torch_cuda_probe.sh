#!/usr/bin/env bash
set -euo pipefail

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
runtime_handler="${AE_CRI_RUNTIME_HANDLER:-nvidia}"
image="${AE_CRI_PROBE_IMAGE:-}"
namespace="${AE_CRI_PROBE_NAMESPACE:-k1s-cuda-probe}"
probe_timeout="${AE_CRI_PROBE_TIMEOUT:-180s}"
crictl_bin="${CRICTL_BIN:-crictl}"

if [[ -z "$image" ]]; then
  echo "AE_CRI_PROBE_IMAGE must be set" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
else
  echo "python3 or python not found; required for JSON escaping and CRI inspect fallback" >&2
  exit 1
fi

if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found; install it to run CUDA image probes" >&2
  exit 1
fi

now_ms() {
  "$python_bin" - <<'PY'
import time

print(int(time.time() * 1000))
PY
}

trim_json_text() {
  local text="${1:-}"
  local limit="${2:-12000}"
  TEXT="$text" LIMIT="$limit" "$python_bin" - <<'PY'
import os

text = str(os.environ.get("TEXT", "") or "")
limit = int(os.environ.get("LIMIT", "12000") or "12000")
if len(text) <= limit:
    print(text)
else:
    head = max(1, limit // 2)
    tail = max(1, limit - head - len("\n...\n"))
    print(f"{text[:head]}\n...\n{text[-tail:]}")
PY
}

extract_json_payload() {
  local text="${1:-}"
  TEXT="$text" \
  "$python_bin" - <<'PY'
import json
import os

for raw in reversed(str(os.environ.get("TEXT", "") or "").splitlines()):
    line = raw.strip()
    if not line or not line.startswith("{"):
        continue
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(payload, dict):
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

tmp_dir="$(mktemp -d)"
debug_dir="$tmp_dir/debug"
durations_file="$tmp_dir/durations.tsv"
mkdir -p "$debug_dir" "$tmp_dir/logs"
: >"$durations_file"

probe_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
probe_started_journal="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
probe_started_ms="$(now_ms)"
probe_phase="init"
probe_status="failed"
probe_error=""
probe_result_json=""
pod_name=""
pod_id=""
container_id=""
container_name=""

cleanup() {
  if [[ -n "$container_id" ]]; then
    "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$pod_id" ]]; then
    "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" stopp "$pod_id" >/dev/null 2>&1 || true
    "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" rmp "$pod_id" >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp_dir"
}

trap cleanup EXIT

emit_stage_marker() {
  local stage="$1"
  local status="$2"
  local elapsed_ms="$3"
  printf '__probe_stage__ phase=%s status=%s elapsed_ms=%s\n' "$stage" "$status" "$elapsed_ms" >&2
}

append_stage_duration() {
  local stage="$1"
  local elapsed_ms="$2"
  printf '%s\t%s\n' "$stage" "$elapsed_ms" >>"$durations_file"
}

run_crictl_stage() {
  local stage="$1"
  shift
  local start_ms end_ms elapsed_ms rc output
  start_ms="$(now_ms)"
  set +e
  output="$("$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" "$@" 2>&1)"
  rc=$?
  set -e
  end_ms="$(now_ms)"
  elapsed_ms=$((end_ms - start_ms))
  append_stage_duration "$stage" "$elapsed_ms"
  if [[ "$rc" -eq 0 ]]; then
    emit_stage_marker "$stage" "ok" "$elapsed_ms"
  else
    emit_stage_marker "$stage" "failed" "$elapsed_ms"
  fi
  printf '%s' "$output"
  return "$rc"
}

duration_to_ms() {
  local value="${1:-180s}"
  VALUE="$value" "$python_bin" - <<'PY'
import os
import re

value = str(os.environ.get("VALUE", "180s") or "180s").strip().lower()
match = re.fullmatch(r"(\d+)(ms|s|m|h)?", value)
if not match:
    print(180000)
    raise SystemExit(0)
amount = int(match.group(1))
unit = match.group(2) or "s"
multiplier = {
    "ms": 1,
    "s": 1000,
    "m": 60 * 1000,
    "h": 60 * 60 * 1000,
}[unit]
print(amount * multiplier)
PY
}

extract_last_nonempty_line() {
  local value="${1:-}"
  printf '%s\n' "$value" | awk 'NF { line=$0 } END { if (line != "") print line }'
}

extract_hex_id() {
  local value
  value="$(extract_last_nonempty_line "${1:-}" | tr -d '\r')"
  if [[ "$value" =~ ^[[:xdigit:]]{16,}$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  return 1
}

extract_numeric_exit_code() {
  local value
  value="$(extract_last_nonempty_line "${1:-}" | tr -d '\r')"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  return 1
}

container_state_field() {
  local inspect_text="${1:-}"
  local field="${2:-state}"
  INSPECT_TEXT="$inspect_text" FIELD="$field" "$python_bin" - <<'PY'
import json
import os

text = str(os.environ.get("INSPECT_TEXT", "") or "")
field = str(os.environ.get("FIELD", "state") or "state")
payload = json.loads(text)
status = payload.get("status") or {}
state = str(status.get("state") or "").strip()
exit_code = status.get("exitCode")
reason = str(status.get("reason") or "").strip()
message = str(status.get("message") or "").strip()

if field == "state":
    if state:
        print(state)
elif field == "exit_code":
    if isinstance(exit_code, int):
        print(exit_code)
    elif isinstance(exit_code, str) and exit_code.strip().isdigit():
        print(exit_code.strip())
elif field == "reason":
    if reason:
        print(reason)
elif field == "message":
    if message:
        print(message)
PY
}

capture_debug_file() {
  local name="$1"
  shift
  set +e
  "$@" >"$debug_dir/$name.txt" 2>&1
  set -e
}

capture_debug_snapshot() {
  capture_debug_file image_inspect \
    "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" inspecti "$image"
  capture_debug_file crictl_images \
    "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" images
  if command -v ctr >/dev/null 2>&1; then
    capture_debug_file ctr_images ctr -n k8s.io images ls
  fi
  if [[ -n "$pod_id" ]]; then
    capture_debug_file pod_inspect \
      "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" inspectp "$pod_id"
  fi
  if [[ -n "$container_id" ]]; then
    capture_debug_file container_inspect \
      "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" inspect "$container_id"
    capture_debug_file container_logs \
      "$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" logs "$container_id"
  fi
  if command -v journalctl >/dev/null 2>&1; then
    capture_debug_file containerd_journal \
      journalctl -u containerd --since "$probe_started_journal" -n 200 --no-pager
  fi
}

emit_final_payload() {
  local exit_code="$1"
  local total_ms
  total_ms=$(( $(now_ms) - probe_started_ms ))
  PHASE="$probe_phase" \
  STATUS="$probe_status" \
  IMAGE="$image" \
  RUNTIME_HANDLER="$runtime_handler" \
  TIMEOUT="$probe_timeout" \
  STARTED_AT="$probe_started_at" \
  DURATION_MS="$total_ms" \
  POD_ID="$pod_id" \
  CONTAINER_ID="$container_id" \
  ERROR_TEXT="$probe_error" \
  RESULT_JSON="$probe_result_json" \
  DEBUG_DIR="$debug_dir" \
  DURATIONS_FILE="$durations_file" \
  EXIT_CODE="$exit_code" \
  "$python_bin" - <<'PY'
import json
import os
from pathlib import Path


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - len("\n...\n"))
    return f"{text[:head]}\n...\n{text[-tail:]}"


payload: dict[str, object] = {
    "status": os.environ.get("STATUS", "failed"),
    "phase": os.environ.get("PHASE", "unknown"),
    "image": os.environ.get("IMAGE", ""),
    "runtime_handler": os.environ.get("RUNTIME_HANDLER", ""),
    "timeout": os.environ.get("TIMEOUT", ""),
    "started_at": os.environ.get("STARTED_AT", ""),
    "duration_ms": int(os.environ.get("DURATION_MS", "0") or "0"),
    "exit_code": int(os.environ.get("EXIT_CODE", "1") or "1"),
}
pod_id = str(os.environ.get("POD_ID", "") or "").strip()
container_id = str(os.environ.get("CONTAINER_ID", "") or "").strip()
error_text = str(os.environ.get("ERROR_TEXT", "") or "").strip()
if pod_id:
    payload["pod_id"] = pod_id
if container_id:
    payload["container_id"] = container_id
if error_text:
    payload["error"] = error_text

durations: dict[str, int] = {}
durations_path = Path(os.environ.get("DURATIONS_FILE", ""))
if durations_path.is_file():
    for raw in durations_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        stage, elapsed = raw.split("\t", 1)
        try:
            durations[stage] = int(elapsed)
        except ValueError:
            continue
if durations:
    payload["durations_ms"] = durations

result_json = str(os.environ.get("RESULT_JSON", "") or "").strip()
if result_json:
    try:
        payload["result"] = json.loads(result_json)
    except json.JSONDecodeError:
        payload["result"] = {"status": "failed", "error": result_json}

debug: dict[str, str] = {}
debug_dir = Path(os.environ.get("DEBUG_DIR", ""))
if debug_dir.is_dir():
    for name in (
        "image_inspect",
        "pod_inspect",
        "container_inspect",
        "container_logs",
        "crictl_images",
        "ctr_images",
        "containerd_journal",
    ):
        text = read_text(debug_dir / f"{name}.txt")
        if text:
            debug[name] = text
if debug:
    payload["debug"] = debug

print(json.dumps(payload, sort_keys=True))
PY
  exit "$exit_code"
}

info_output="$(run_crictl_stage info info)" || {
  probe_phase="info"
  probe_error="$(trim_json_text "$info_output")"
  capture_debug_snapshot
  emit_final_payload 1
}

inspect_output="$(run_crictl_stage inspecti inspecti "$image")" || {
  probe_phase="inspecti"
  probe_error="$(trim_json_text "$inspect_output")"
  capture_debug_snapshot
  emit_final_payload 1
}
printf '%s\n' "$inspect_output" >"$debug_dir/image_inspect.txt"

python_probe="$(
  cat <<'PY'
import json

payload = {}
try:
    import torch

    payload["torch_version"] = str(getattr(torch, "__version__", ""))
    payload["cuda_version"] = str(getattr(torch.version, "cuda", ""))
    try:
        payload["cuda_device_count"] = int(torch.cuda.device_count())
    except Exception as exc:  # noqa: BLE001
        payload["cuda_device_count"] = 0
        payload["cuda_device_count_error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        payload["cuda_is_available"] = bool(torch.cuda.is_available())
    except Exception as exc:  # noqa: BLE001
        payload["cuda_is_available"] = False
        payload["cuda_is_available_error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        probe = torch.zeros(1, device="cuda")
        payload["cuda_tensor_ok"] = True
        payload["cuda_tensor_device"] = str(getattr(probe, "device", "cuda"))
    except Exception as exc:  # noqa: BLE001
        payload["cuda_tensor_ok"] = False
        payload["cuda_error"] = f"{exc.__class__.__name__}: {exc}"
    payload["status"] = (
        "ready"
        if bool(payload.get("cuda_is_available")) and bool(payload.get("cuda_tensor_ok"))
        else "failed"
    )
except Exception as exc:  # noqa: BLE001
    payload["status"] = "failed"
    payload["error"] = f"{exc.__class__.__name__}: {exc}"

print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload.get("status") == "ready" else 1)
PY
)"
python_probe_json="$(
  printf '%s' "$python_probe" | "$python_bin" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
)"

pod_name="cri-cuda-probe-$(date -u +%Y%m%d%H%M%S)-$$"
container_name="${pod_name}-probe"

cat >"$tmp_dir/pod.json" <<EOF
{
  "metadata": {
    "name": "${pod_name}",
    "namespace": "${namespace}",
    "uid": "${pod_name}",
    "attempt": 0
  },
  "log_directory": "${tmp_dir}/logs",
  "linux": {}
}
EOF

cat >"$tmp_dir/container.json" <<EOF
{
  "metadata": {
    "name": "${container_name}"
  },
  "image": {
    "image": "${image}"
  },
  "command": ["python3"],
  "args": ["-c", ${python_probe_json}],
  "log_path": "${container_name}.log",
  "linux": {}
}
EOF

runp_output="$(run_crictl_stage runp runp -r "$runtime_handler" "$tmp_dir/pod.json")" || {
  probe_phase="runp"
  probe_error="$(trim_json_text "$runp_output")"
  capture_debug_snapshot
  emit_final_payload 1
}
if pod_id="$(extract_hex_id "$runp_output")"; then
  :
else
  probe_phase="runp"
  probe_error="CUDA image probe failed: runp returned an invalid pod sandbox id: $(trim_json_text "$runp_output")"
  capture_debug_snapshot
  emit_final_payload 1
fi

create_output="$(run_crictl_stage create create "$pod_id" "$tmp_dir/container.json" "$tmp_dir/pod.json")" || {
  probe_phase="create"
  probe_error="$(trim_json_text "$create_output")"
  capture_debug_snapshot
  emit_final_payload 1
}
if container_id="$(extract_hex_id "$create_output")"; then
  :
else
  probe_phase="create"
  probe_error="CUDA image probe failed: create returned an invalid container id: $(trim_json_text "$create_output")"
  capture_debug_snapshot
  emit_final_payload 1
fi

start_output="$(run_crictl_stage start start "$container_id")" || {
  probe_phase="start"
  probe_error="$(trim_json_text "$start_output")"
  capture_debug_snapshot
  emit_final_payload 1
}

wait_start_ms="$(now_ms)"
wait_timeout_ms="$(duration_to_ms "$probe_timeout")"
wait_deadline_ms=$((wait_start_ms + wait_timeout_ms))
wait_output=""
while true; do
  set +e
  wait_output="$("$crictl_bin" --runtime-endpoint "$endpoint" --timeout "$probe_timeout" inspect "$container_id" 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    wait_elapsed_ms=$(( $(now_ms) - wait_start_ms ))
    append_stage_duration "wait" "$wait_elapsed_ms"
    emit_stage_marker "wait" "failed" "$wait_elapsed_ms"
    probe_phase="wait"
    probe_error="$(trim_json_text "$wait_output")"
    capture_debug_snapshot
    emit_final_payload 1
  fi
  printf '%s\n' "$wait_output" >"$debug_dir/container_inspect.txt"
  container_state="$(container_state_field "$wait_output" state 2>/dev/null || true)"
  if [[ -z "$container_state" ]]; then
    wait_elapsed_ms=$(( $(now_ms) - wait_start_ms ))
    append_stage_duration "wait" "$wait_elapsed_ms"
    emit_stage_marker "wait" "failed" "$wait_elapsed_ms"
    probe_phase="wait"
    probe_error="CUDA image probe wait could not determine container state from inspect output"
    capture_debug_snapshot
    emit_final_payload 1
  fi
  if [[ "$container_state" != "CONTAINER_RUNNING" && "$container_state" != "CONTAINER_CREATED" ]]; then
    break
  fi
  if (( $(now_ms) >= wait_deadline_ms )); then
    wait_elapsed_ms=$(( $(now_ms) - wait_start_ms ))
    append_stage_duration "wait" "$wait_elapsed_ms"
    emit_stage_marker "wait" "failed" "$wait_elapsed_ms"
    probe_phase="wait"
    probe_error="CUDA image probe timed out waiting for container exit after ${probe_timeout}"
    capture_debug_snapshot
    emit_final_payload 1
  fi
  sleep 2
done

wait_elapsed_ms=$(( $(now_ms) - wait_start_ms ))
append_stage_duration "wait" "$wait_elapsed_ms"
emit_stage_marker "wait" "ok" "$wait_elapsed_ms"
if exit_code="$(container_state_field "$wait_output" exit_code 2>/dev/null || true)" && [[ -n "$exit_code" ]]; then
  :
else
  probe_phase="wait"
  probe_error="CUDA image probe wait returned an invalid exit code from container inspect"
  capture_debug_snapshot
  emit_final_payload 1
fi

logs_output="$(run_crictl_stage logs logs "$container_id")" || {
  probe_phase="logs"
  probe_error="$(trim_json_text "$logs_output")"
  capture_debug_snapshot
  emit_final_payload 1
}
printf '%s\n' "$logs_output"
printf '%s\n' "$logs_output" >"$debug_dir/container_logs.txt"

if probe_result_json="$(extract_json_payload "$logs_output" 2>/dev/null)"; then
  :
else
  probe_result_json=""
fi

if [[ -n "$probe_result_json" ]]; then
  probe_phase="python"
  probe_status="$(
    RESULT_JSON="$probe_result_json" "$python_bin" - <<'PY'
import json
import os

payload = json.loads(os.environ["RESULT_JSON"])
print(str(payload.get("status") or "failed").strip().lower() or "failed")
PY
  )"
else
  probe_phase="python"
  probe_status="failed"
  probe_error="probe container completed without emitting a JSON payload"
  capture_debug_snapshot
  emit_final_payload 1
fi

if [[ "$probe_status" != "ready" || "$exit_code" != "0" ]]; then
  if [[ -z "$probe_error" ]]; then
    probe_error="CUDA image probe container exited with code ${exit_code}"
  fi
  capture_debug_snapshot
  emit_final_payload 1
fi

printf 'CUDA image probe OK\n' >&2
emit_final_payload 0
