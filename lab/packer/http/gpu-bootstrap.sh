#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
NVIDIA_DRIVER_PACKAGE="${NVIDIA_DRIVER_PACKAGE:-nvidia-driver-535}"

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y \
  "${NVIDIA_DRIVER_PACKAGE}" \
  nvidia-container-toolkit \
  pciutils

if command -v nvidia-ctk >/dev/null 2>&1; then
  nvidia-ctk runtime configure --runtime=containerd --config=/etc/containerd/config.toml || true
fi

systemctl restart containerd

mkdir -p /etc/k1s-image
cat >/etc/k1s-image/gpu-info.json <<JSON
{
  "driver_package": "${NVIDIA_DRIVER_PACKAGE}",
  "runtime": "nvidia"
}
JSON

apt-get clean
rm -rf /var/lib/apt/lists/*
