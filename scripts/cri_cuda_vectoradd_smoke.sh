#!/usr/bin/env bash
set -euo pipefail

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
runtime_handler="${AE_CRI_RUNTIME_HANDLER:-nvidia}"
image="${AE_CRI_VECTORADD_IMAGE:-nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1}"
namespace="${AE_CRI_VECTORADD_NAMESPACE:-k1s-gpu-smoke}"
success_signal="${AE_CRI_VECTORADD_SUCCESS_SIGNAL:-Test PASSED}"
crictl_bin="${CRICTL_BIN:-crictl}"

if command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
else
  echo "python3 or python not found; required for CRI inspect fallback" >&2
  exit 1
fi

if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found; install it to run GPU compute smoke" >&2
  exit 1
fi

tmp_dir=""
pod_name=""
pod_id=""
container_id=""
container_name=""

cleanup() {
  if [[ -n "$container_id" ]]; then
    "$crictl_bin" --runtime-endpoint "$endpoint" rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$pod_id" ]]; then
    "$crictl_bin" --runtime-endpoint "$endpoint" stopp "$pod_id" >/dev/null 2>&1 || true
    "$crictl_bin" --runtime-endpoint "$endpoint" rmp "$pod_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi
}

trap cleanup EXIT

wait_for_container_exit() {
  if "$crictl_bin" help 2>&1 | grep -Eq '^[[:space:]]+wait([[:space:]]|$)'; then
    "$crictl_bin" --runtime-endpoint "$endpoint" wait "$container_id" | tr -d '\r\n'
    return 0
  fi

  local inspect_json parsed state exit_code
  for _ in $(seq 1 120); do
    inspect_json="$("$crictl_bin" --runtime-endpoint "$endpoint" inspect "$container_id")"
    parsed="$(
      "$python_bin" - "$inspect_json" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
status = data.get("status") or {}
print(status.get("state", ""))
print(status.get("exitCode", ""))
PY
    )"
    state="${parsed%%$'\n'*}"
    exit_code="${parsed#*$'\n'}"
    if [[ "$state" == "CONTAINER_EXITED" || "$state" == "CONTAINER_UNKNOWN" ]]; then
      printf '%s\n' "${exit_code:-1}"
      return 0
    fi
    sleep 1
  done

  echo "GPU compute smoke timed out waiting for container exit" >&2
  return 1
}

"$crictl_bin" --runtime-endpoint "$endpoint" info >/dev/null
if ! "$crictl_bin" --runtime-endpoint "$endpoint" inspecti "$image" >/dev/null 2>&1; then
  echo "required seeded GPU image is missing from CRI cache: $image" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
mkdir -p "$tmp_dir/logs"
pod_name="cri-gpu-smoke-$(date -u +%Y%m%d%H%M%S)-$$"
container_name="${pod_name}-vectoradd"

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
  "log_path": "${container_name}.log",
  "linux": {}
}
EOF

pod_id="$("$crictl_bin" --runtime-endpoint "$endpoint" runp -r "$runtime_handler" "$tmp_dir/pod.json" | tr -d '\r\n')"
if [[ -z "$pod_id" ]]; then
  echo "GPU compute smoke failed: runp returned no pod sandbox id" >&2
  exit 1
fi

container_id="$("$crictl_bin" --runtime-endpoint "$endpoint" create "$pod_id" "$tmp_dir/container.json" "$tmp_dir/pod.json" | tr -d '\r\n')"
if [[ -z "$container_id" ]]; then
  echo "GPU compute smoke failed: create returned no container id" >&2
  exit 1
fi

"$crictl_bin" --runtime-endpoint "$endpoint" start "$container_id" >/dev/null
exit_code="$(wait_for_container_exit)"
logs="$("$crictl_bin" --runtime-endpoint "$endpoint" logs "$container_id" 2>&1 || true)"
printf '%s\n' "$logs"

if [[ "$exit_code" != "0" ]]; then
  echo "GPU compute smoke container exited with code ${exit_code}" >&2
  exit 1
fi

if ! printf '%s\n' "$logs" | grep -F -- "$success_signal" >/dev/null 2>&1; then
  echo "GPU compute smoke missing success signal: ${success_signal}" >&2
  exit 1
fi

echo "GPU compute smoke OK (image=${image}, runtime_handler=${runtime_handler})"
