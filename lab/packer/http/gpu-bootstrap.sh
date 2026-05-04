#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
NVIDIA_DRIVER_PACKAGE="${NVIDIA_DRIVER_PACKAGE:-nvidia-driver-535}"
CRI_ENDPOINT="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
NVIDIA_RUNTIME_HANDLER="${AE_CRI_RUNTIME_HANDLER:-nvidia}"
GPU_REQUIRED_CRI_IMAGES="${GPU_REQUIRED_CRI_IMAGES:-nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1}"
GPU_CONTRACT_SCRIPT="${GPU_CONTRACT_SCRIPT:-/usr/local/bin/k1s-gpu-contract-check}"

require_command() {
  local command_name="$1"
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "required command missing after GPU bootstrap: $command_name" >&2
    exit 1
  }
}

assert_runtime_handler_available() {
  local info_file handler_name
  handler_name="$1"
  info_file="$(mktemp)"
  crictl --runtime-endpoint "$CRI_ENDPOINT" info >"$info_file"
  if ! jq -e --arg handler "$handler_name" '
    (
      ((.config.containerd.runtimes // {}) | keys)
      + [(.config.containerd.defaultRuntimeName // .config.containerd.default_runtime_name // empty)]
    )
    | unique
    | index($handler)
  ' "$info_file" >/dev/null; then
    echo "required CRI runtime handler unavailable after GPU bootstrap: ${handler_name}" >&2
    rm -f "$info_file"
    exit 1
  fi
  rm -f "$info_file"
}

assert_seed_images_cached() {
  local image
  for image in $GPU_REQUIRED_CRI_IMAGES; do
    ctr -n k8s.io images ls name=="$image" | grep -F -- "$image" >/dev/null 2>&1 || {
      echo "required seeded GPU image missing from CRI cache: ${image}" >&2
      exit 1
    }
  done
}

write_gpu_contract_script() {
  cat >"$GPU_CONTRACT_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

cri_endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
runtime_handler="${AE_CRI_RUNTIME_HANDLER:-nvidia}"
nvidia_driver_package="${NVIDIA_DRIVER_PACKAGE:-nvidia-driver-535}"
required_images="${GPU_REQUIRED_CRI_IMAGES:-nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1}"

require_command() {
  local command_name="$1"
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "required command missing: ${command_name}" >&2
    exit 1
  }
}

require_package() {
  local package_name="$1"
  dpkg-query -W -f='${Status}\n' "$package_name" 2>/dev/null | grep -Fqx "install ok installed" || {
    echo "required package missing: ${package_name}" >&2
    exit 1
  }
}

assert_runtime_handler_available() {
  local info_file
  info_file="$(mktemp)"
  crictl --runtime-endpoint "$cri_endpoint" info >"$info_file"
  if ! jq -e --arg handler "$runtime_handler" '
    (
      ((.config.containerd.runtimes // {}) | keys)
      + [(.config.containerd.defaultRuntimeName // .config.containerd.default_runtime_name // empty)]
    )
    | unique
    | index($handler)
  ' "$info_file" >/dev/null; then
    echo "required CRI runtime handler unavailable: ${runtime_handler}" >&2
    rm -f "$info_file"
    exit 1
  fi
  rm -f "$info_file"
}

assert_seed_images_cached() {
  local image
  for image in $required_images; do
    ctr -n k8s.io images ls name=="$image" | grep -F -- "$image" >/dev/null 2>&1 || {
      echo "required seeded GPU image missing from CRI cache: ${image}" >&2
      exit 1
    }
  done
}

require_command crictl
require_command ctr
require_command jq
require_command nvidia-ctk
require_command nvidia-container-runtime
require_command nvidia-smi

test -f /etc/k1s-image/gpu-info.json
test -f /etc/apt/sources.list.d/nvidia-container-toolkit.list
require_package "$nvidia_driver_package"
require_package nvidia-container-toolkit

assert_runtime_handler_available
assert_seed_images_cached
EOF
  chmod +x "$GPU_CONTRACT_SCRIPT"
}

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y \
  "${NVIDIA_DRIVER_PACKAGE}" \
  nvidia-container-toolkit \
  pciutils

require_command nvidia-ctk
require_command nvidia-container-runtime
require_command nvidia-smi

nvidia-ctk runtime configure --runtime=containerd --config=/etc/containerd/config.toml

systemctl restart containerd

mkdir -p /etc/k1s-image
cat >/etc/k1s-image/gpu-info.json <<JSON
{
  "driver_package": "${NVIDIA_DRIVER_PACKAGE}",
  "runtime": "nvidia"
}
JSON

write_gpu_contract_script
export NVIDIA_DRIVER_PACKAGE
export AE_CRI_ENDPOINT="$CRI_ENDPOINT"
export AE_CRI_RUNTIME_HANDLER="$NVIDIA_RUNTIME_HANDLER"
export GPU_REQUIRED_CRI_IMAGES
echo "[image-bootstrap] validating gpu contract script"
"$GPU_CONTRACT_SCRIPT"
echo "[image-bootstrap] validating gpu runtime handler"
assert_runtime_handler_available "$NVIDIA_RUNTIME_HANDLER"
echo "[image-bootstrap] validating gpu seed image cache"
assert_seed_images_cached

apt-get clean
rm -rf /var/lib/apt/lists/*
