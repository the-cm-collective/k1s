#!/usr/bin/env bash
set -euo pipefail

variant="${1:-base}"
seed_manifest="${2:-/tmp/cri_seed_images.lock.json}"
export DEBIAN_FRONTEND=noninteractive
crictl_version="${CRICTL_VERSION:-v1.30.0}"

crictl_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *)
      echo "unsupported arch for crictl install: $(uname -m)" >&2
      return 1
      ;;
  esac
}

install_crictl_binary() {
  local arch url tmp
  arch="$(crictl_arch)"
  url="https://github.com/kubernetes-sigs/cri-tools/releases/download/${crictl_version}/crictl-${crictl_version}-linux-${arch}.tar.gz"
  tmp="$(mktemp -d)"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmp/crictl.tar.gz"
  else
    echo "curl is required to install crictl fallback binary" >&2
    return 1
  fi

  tar -C /usr/local/bin -xzf "$tmp/crictl.tar.gz"
  chmod +x /usr/local/bin/crictl
  rm -rf "$tmp"
}

write_cni_configs() {
  mkdir -p /etc/cni/net.d
  cat >/etc/cni/net.d/10-k1s-bridge.conflist <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "cni0",
  "plugins": [
    {
      "type": "bridge",
      "bridge": "cni0",
      "isGateway": true,
      "ipMasq": true,
      "promiscMode": true,
      "ipam": {
        "type": "host-local",
        "ranges": [[{ "subnet": "10.88.0.0/16" }]],
        "routes": [{ "dst": "0.0.0.0/0" }]
      }
    },
    { "type": "portmap", "capabilities": { "portMappings": true } },
    { "type": "firewall" },
    { "type": "tuning" }
  ]
}
EOF
  cat >/etc/cni/net.d/99-loopback.conf <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "lo",
  "type": "loopback"
}
EOF
}

apt-get update
apt-get install -y \
  apt-transport-https \
  ca-certificates \
  cloud-init \
  containernetworking-plugins \
  containerd \
  curl \
  gnupg \
  iptables \
  jq \
  linux-generic \
  lsb-release \
  python-is-python3 \
  python3 \
  python3-pip \
  qemu-guest-agent

if ! command -v crictl >/dev/null 2>&1; then
  if ! apt-get install -y cri-tools; then
    echo "[image-bootstrap] cri-tools package unavailable; installing crictl ${crictl_version} binary"
    install_crictl_binary
  fi
fi

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
if ! containerd config dump --config /etc/containerd/config.toml >/dev/null 2>&1; then
  containerd config default >/etc/containerd/config.toml
fi

cat >/etc/crictl.yaml <<'EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
debug: false
EOF

if [[ -d /usr/lib/cni ]]; then
  mkdir -p /opt/cni/bin
  cp -a /usr/lib/cni/. /opt/cni/bin/
fi
write_cni_configs

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
  "cri_seed_version": "${seed_version}",
  "vm_bootstrap_ready": true,
  "python_alias": true,
  "crictl_ready": true,
  "cni_ready": true
}
JSON

if [[ -f "$seed_manifest" ]]; then
  cp "$seed_manifest" /etc/k1s-image/cri_seed_images.lock.json
fi

apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
