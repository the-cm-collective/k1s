#!/usr/bin/env bash
set -euo pipefail

variant="${1:-base}"
seed_manifest="${2:-/tmp/cri_seed_images.lock.json}"
seed_bundle="${3:-/tmp/cri-seed-images.oci.tar}"
cri_smoke_script="${4:-/tmp/cri_smoke.sh}"
export DEBIAN_FRONTEND=noninteractive
crictl_version="${CRICTL_VERSION:-v1.30.0}"
bootstrap_contract_version="${BOOTSTRAP_CONTRACT_VERSION:-20260324-cni-0.4.0-smoke-v1}"
expected_cni_version="${AE_CNI_VERSION:-0.4.0}"
sandbox_image="${AE_CRI_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"

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
  "cniVersion": "0.4.0",
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
  "cniVersion": "0.4.0",
  "name": "lo",
  "type": "loopback"
}
EOF
}

write_containerd_bootstrap_config() {
  mkdir -p /etc/containerd/conf.d
  cat >/etc/containerd/conf.d/10-k1s-bootstrap.toml <<EOF
[plugins.'io.containerd.cri.v1.images'.pinned_images]
  sandbox = '${sandbox_image}'

[plugins.'io.containerd.grpc.v1.cri']
  sandbox_image = '${sandbox_image}'
EOF
}

ensure_initramfs_module() {
  local module="$1"
  touch /etc/initramfs-tools/modules
  grep -qxF "$module" /etc/initramfs-tools/modules || printf '%s\n' "$module" >> /etc/initramfs-tools/modules
}

write_virtio_root_modules() {
  mkdir -p /etc/initramfs-tools
  # Keep the golden image boot path aligned with the verifier and lab VMs,
  # which attach the root qcow through virtio block devices.
  ensure_initramfs_module virtio
  ensure_initramfs_module virtio_pci
  ensure_initramfs_module virtio_blk
  ensure_initramfs_module virtio_ring
}

guest_root_uuid() {
  findmnt -no UUID /
}

guest_root_label() {
  findmnt -no LABEL / || true
}

guest_fstab_root_source() {
  awk '
    /^[[:space:]]*#/ { next }
    $2 == "/" { print $1; exit }
  ' /etc/fstab
}

guest_grub_root_uuids() {
  grep -oE 'root=UUID=[^"[:space:]]+' /boot/grub/grub.cfg |
    sed 's/root=UUID=//' |
    awk 'NF && !seen[$0]++ { print $0 }'
}

assert_guest_boot_contract() {
  local live_uuid="" live_label="" fstab_source="" fstab_uuid="" fstab_label="" grub_uuid=""
  local -a grub_uuids=()

  update-initramfs -u -k all
  update-grub

  live_uuid="$(guest_root_uuid)"
  [[ -n "$live_uuid" ]] || {
    echo "[image-bootstrap] unable to determine live root UUID" >&2
    return 1
  }

  fstab_source="$(guest_fstab_root_source)"
  [[ -n "$fstab_source" ]] || {
    echo "[image-bootstrap] /etc/fstab missing root filesystem entry" >&2
    return 1
  }
  if [[ "$fstab_source" == UUID=* ]]; then
    fstab_uuid="${fstab_source#UUID=}"
    [[ "$fstab_uuid" == "$live_uuid" ]] || {
      echo "[image-bootstrap] /etc/fstab root UUID mismatch: expected ${live_uuid}, found ${fstab_uuid}" >&2
      return 1
    }
  elif [[ "$fstab_source" == LABEL=* ]]; then
    live_label="$(guest_root_label)"
    [[ -n "$live_label" ]] || {
      echo "[image-bootstrap] unable to determine live root label for LABEL-based /etc/fstab entry" >&2
      return 1
    }
    fstab_label="${fstab_source#LABEL=}"
    [[ "$fstab_label" == "$live_label" ]] || {
      echo "[image-bootstrap] /etc/fstab root LABEL mismatch: expected ${live_label}, found ${fstab_label}" >&2
      return 1
    }
  else
    echo "[image-bootstrap] /etc/fstab root entry must use UUID=... or LABEL=... (found: ${fstab_source})" >&2
    return 1
  fi

  mapfile -t grub_uuids < <(guest_grub_root_uuids)
  [[ "${#grub_uuids[@]}" -gt 0 ]] || {
    echo "[image-bootstrap] /boot/grub/grub.cfg missing root=UUID entries" >&2
    return 1
  }

  for grub_uuid in "${grub_uuids[@]}"; do
    [[ "$grub_uuid" == "$live_uuid" ]] || {
      echo "[image-bootstrap] grub root UUID mismatch: expected ${live_uuid}, found ${grub_uuid}" >&2
      return 1
    }
  done
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
  echo "[image-bootstrap] installing crictl ${crictl_version} binary"
  install_crictl_binary
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
if ! containerd --config /etc/containerd/config.toml config dump >/dev/null 2>&1; then
  containerd config default >/etc/containerd/config.toml
fi
write_containerd_bootstrap_config

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
write_virtio_root_modules

# Let runtime cloud-init activate qemu-guest-agent on first boot; the golden
# image bake only needs containerd running for CRI validation and cache seeding.
systemctl enable containerd
systemctl restart containerd

seed_version=""
if [[ -f "$seed_manifest" ]]; then
  seed_version="$(jq -r '.seed_version // empty' "$seed_manifest" 2>/dev/null || true)"
fi

[[ -f "$seed_bundle" ]] || {
  echo "[image-bootstrap] missing CRI seed bundle: $seed_bundle" >&2
  exit 1
}
[[ -x "$cri_smoke_script" ]] || {
  echo "[image-bootstrap] missing CRI smoke script: $cri_smoke_script" >&2
  exit 1
}

echo "[image-bootstrap] importing CRI seed bundle from $seed_bundle"
ctr -n k8s.io images import "$seed_bundle"

echo "[image-bootstrap] validating CRI sandbox image: ${sandbox_image}"
AE_CRI_SANDBOX_IMAGE="$sandbox_image" AE_CRI_SMOKE_PULL=0 "$cri_smoke_script"

if [[ -f "$seed_manifest" ]]; then
  echo "[image-bootstrap] recorded CRI seed manifest from $seed_manifest"
fi

mkdir -p /etc/k1s-image
cat >/etc/k1s-image/build-info.json <<JSON
{
  "variant": "${variant}",
  "distro": "ubuntu-22.04",
  "kernel_track": "ga-5.15",
  "cri_seed_version": "${seed_version}",
  "bootstrap_contract_version": "${bootstrap_contract_version}",
  "cni_version": "${expected_cni_version}",
  "vm_bootstrap_ready": true,
  "python_alias": true,
  "crictl_ready": true,
  "cni_ready": true
}
JSON

if [[ -f "$seed_manifest" ]]; then
  cp "$seed_manifest" /etc/k1s-image/cri_seed_images.lock.json
fi

assert_guest_boot_contract

apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
