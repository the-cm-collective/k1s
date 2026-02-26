#!/usr/bin/env bash
set -euo pipefail

variant="${1:-base}"
seed_manifest="${2:-/tmp/cri_seed_images.lock.json}"
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

seed_version=""
if [[ -f "$seed_manifest" ]]; then
  seed_version="$(jq -r '.seed_version // empty' "$seed_manifest" 2>/dev/null || true)"
  echo "[image-bootstrap] pre-seeding CRI cache from $seed_manifest"
  mapfile -t seed_images < <(jq -r '[.images.core[]?, .images.edge[]?] | unique[]' "$seed_manifest")
  for image in "${seed_images[@]}"; do
    [[ -n "$image" ]] || continue
    if ctr -n k8s.io images inspect "$image" >/dev/null 2>&1; then
      echo "[image-bootstrap] CRI image already cached: $image"
      continue
    fi
    echo "[image-bootstrap] pull CRI image: $image"
    ctr -n k8s.io images pull --platform linux/amd64 "$image"
  done
fi

mkdir -p /etc/k1s-image
cat >/etc/k1s-image/build-info.json <<JSON
{
  "variant": "${variant}",
  "distro": "ubuntu-22.04",
  "kernel_track": "ga-5.15",
  "cri_seed_version": "${seed_version}"
}
JSON

if [[ -f "$seed_manifest" ]]; then
  cp "$seed_manifest" /etc/k1s-image/cri_seed_images.lock.json
fi

apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
