#!/usr/bin/env bash
set -euo pipefail

variant="${1:-base}"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  apt-transport-https \
  ca-certificates \
  cloud-init \
  containerd \
  curl \
  gnupg \
  jq \
  linux-generic \
  lsb-release \
  python3 \
  python3-pip \
  qemu-guest-agent

# Keep Ubuntu 22.04 on the GA kernel line (5.15) for vGPU stability work.
apt-get purge -y linux-generic-hwe-22.04 linux-image-generic-hwe-22.04 linux-headers-generic-hwe-22.04 || true
apt-mark hold linux-generic linux-image-generic linux-headers-generic || true
apt-mark hold linux-generic-hwe-22.04 linux-image-generic-hwe-22.04 linux-headers-generic-hwe-22.04 || true

mkdir -p /etc/apt/apt.conf.d
cat >/etc/apt/apt.conf.d/99k1s-kernel-guard <<'CFG'
Unattended-Upgrade::Package-Blacklist {
  "linux-generic-hwe-22.04";
  "linux-image-generic-hwe-22.04";
  "linux-headers-generic-hwe-22.04";
};
CFG

mkdir -p /etc/containerd
if [[ ! -f /etc/containerd/config.toml ]]; then
  containerd config default >/etc/containerd/config.toml
fi

systemctl enable containerd qemu-guest-agent
systemctl restart containerd qemu-guest-agent

mkdir -p /etc/k1s-image
cat >/etc/k1s-image/build-info.json <<JSON
{
  "variant": "${variant}",
  "distro": "ubuntu-22.04",
  "kernel_track": "ga-5.15"
}
JSON

apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
