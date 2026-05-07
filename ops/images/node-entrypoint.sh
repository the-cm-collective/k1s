#!/bin/sh
set -eu

HOST_NERDCTL_DIR="${AE_NERDCTL_HOST_DIR:-/var/lib/ae/nerdctl-bin}"
HOST_NERDCTL_BIN="${AE_NERDCTL_BIN:-${HOST_NERDCTL_DIR}/nerdctl}"
SOURCE_NERDCTL="/usr/local/bin/nerdctl"
IMAGE_CNI_BIN_DIR="/usr/lib/cni"
CNI_BIN_RUNTIME_DIR="${AE_CONTAINERD_CNI_BIN_DIR:-/var/lib/ae/cni/bin}"
CNI_CONF_SOURCE_DIR="${AE_CONTAINERD_CNI_CONF_SOURCE_DIR:-/etc/cni/net.d}"
CNI_CONF_RUNTIME_DIR="${AE_CONTAINERD_CNI_CONF_DIR:-/var/lib/ae/cni/net.d}"
NVIDIA_TOOLKIT_DIR="${AE_NVIDIA_TOOLKIT_DIR:-}"
NVIDIA_LIBRARY_DIRS="${AE_NVIDIA_LIBRARY_DIRS:-}"
NVIDIA_HOST_LIB_SOURCE_DIR="${AE_NVIDIA_HOST_LIB_SOURCE_DIR:-}"
NVIDIA_LIBRARY_DIR="${AE_NVIDIA_LIBRARY_DIR:-}"

prepend_path() {
  key="$1"
  prefix="$2"
  eval "current=\${$key:-}"
  case ":${current}:" in
    *":${prefix}:"*) return 0 ;;
  esac
  if [ -n "${current}" ]; then
    eval "export ${key}=\"${prefix}:${current}\""
  else
    eval "export ${key}=\"${prefix}\""
  fi
}

if [ -n "${NVIDIA_LIBRARY_DIR}" ]; then
  mkdir -p "${NVIDIA_LIBRARY_DIR}"
  find "${NVIDIA_LIBRARY_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} \; 2>/dev/null || true
  if [ -n "${NVIDIA_HOST_LIB_SOURCE_DIR}" ] && [ -d "${NVIDIA_HOST_LIB_SOURCE_DIR}" ]; then
    for pattern in libnvidia-ml.so* libcuda.so*; do
      for src in "${NVIDIA_HOST_LIB_SOURCE_DIR}"/${pattern}; do
        [ -e "${src}" ] || continue
        cp -a "${src}" "${NVIDIA_LIBRARY_DIR}/"
      done
    done
  fi
fi

if [ -n "${NVIDIA_TOOLKIT_DIR}" ] && [ -d "${NVIDIA_TOOLKIT_DIR}" ]; then
  prepend_path PATH "${NVIDIA_TOOLKIT_DIR}"
fi

if [ -n "${NVIDIA_LIBRARY_DIRS}" ]; then
  old_ifs="${IFS}"
  IFS=":"
  for libdir in ${NVIDIA_LIBRARY_DIRS}; do
    [ -n "${libdir}" ] && [ -d "${libdir}" ] && prepend_path LD_LIBRARY_PATH "${libdir}"
  done
  IFS="${old_ifs}"
fi

if [ -x "${SOURCE_NERDCTL}" ]; then
  mkdir -p "${HOST_NERDCTL_DIR}"
  tmp_bin="${HOST_NERDCTL_BIN}.tmp"
  cp "${SOURCE_NERDCTL}" "${tmp_bin}"
  chmod 0755 "${tmp_bin}"
  mv "${tmp_bin}" "${HOST_NERDCTL_BIN}"
fi

mkdir -p "${CNI_BIN_RUNTIME_DIR}" "${CNI_CONF_RUNTIME_DIR}"
find "${CNI_BIN_RUNTIME_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} \; 2>/dev/null || true

if [ -d "${IMAGE_CNI_BIN_DIR}" ]; then
  cp -a "${IMAGE_CNI_BIN_DIR}"/. "${CNI_BIN_RUNTIME_DIR}"/ 2>/dev/null || true
fi

if [ -d "${CNI_CONF_SOURCE_DIR}" ]; then
  cp -a "${CNI_CONF_SOURCE_DIR}"/. "${CNI_CONF_RUNTIME_DIR}"/ 2>/dev/null || true
fi

exec python -m ae.node "$@"
