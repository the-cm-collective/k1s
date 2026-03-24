#!/usr/bin/env bash
set -euo pipefail

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
image="${AE_CRI_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"
namespace="${AE_CRI_SMOKE_NAMESPACE:-k1s-smoke}"
pull_image="${AE_CRI_SMOKE_PULL:-1}"

crictl_bin="${CRICTL_BIN:-crictl}"
if ! command -v "$crictl_bin" >/dev/null 2>&1; then
  echo "crictl not found; install it to run CRI smoke checks" >&2
  exit 1
fi

runtime_handler="${AE_CRI_RUNTIME_HANDLER:-}"
tmp_dir=""
pod_name=""
pod_id=""

cleanup() {
  local ids=()
  local id=""
  if [[ -n "$pod_id" ]]; then
    ids=("$pod_id")
  elif [[ -n "$pod_name" ]]; then
    mapfile -t ids < <(
      "$crictl_bin" --runtime-endpoint "$endpoint" pods --name "^${pod_name}$" -q 2>/dev/null || true
    )
  fi
  for id in "${ids[@]}"; do
    [[ -n "$id" ]] || continue
    "$crictl_bin" --runtime-endpoint "$endpoint" stopp "$id" >/dev/null 2>&1 || true
    "$crictl_bin" --runtime-endpoint "$endpoint" rmp "$id" >/dev/null 2>&1 || true
  done
  if [[ -n "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi
}

trap cleanup EXIT

$crictl_bin --runtime-endpoint "$endpoint" info >/dev/null
image_note="using $image"
if [[ "$pull_image" == "1" ]]; then
  $crictl_bin --runtime-endpoint "$endpoint" pull "$image" >/dev/null
  image_note="pulled $image"
fi

tmp_dir="$(mktemp -d)"
mkdir -p "$tmp_dir/logs"
pod_name="cri-smoke-$(date -u +%Y%m%d%H%M%S)-$$"

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

runp_cmd=("$crictl_bin" "--runtime-endpoint" "$endpoint" "runp")
if [[ -n "$runtime_handler" ]]; then
  runp_cmd+=("-r" "$runtime_handler")
fi
runp_cmd+=("$tmp_dir/pod.json")
pod_id="$("${runp_cmd[@]}" | tr -d '\r\n')"
if [[ -z "$pod_id" ]]; then
  echo "CRI smoke failed: runp returned no pod sandbox id" >&2
  exit 1
fi

echo "CRI smoke OK (${image_note}, ran PodSandbox $pod_name)"
