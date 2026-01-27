#!/usr/bin/env bash
set -euo pipefail

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
image="${AE_CRI_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"

if ! command -v crictl >/dev/null 2>&1; then
  echo "crictl not found; install it to run CRI smoke checks" >&2
  exit 1
fi

crictl_bin="${CRICTL_BIN:-crictl}"

$crictl_bin --runtime-endpoint "$endpoint" info >/dev/null
$crictl_bin --runtime-endpoint "$endpoint" pull "$image" >/dev/null

echo "CRI smoke OK (pulled $image)"
