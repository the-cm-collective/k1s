#!/usr/bin/env bash
set -euo pipefail

ci_append_path() {
  local dir="${1:?path is required}"
  mkdir -p "$dir"
  if [[ -n "${GITHUB_PATH:-}" ]]; then
    printf '%s\n' "$dir" >>"$GITHUB_PATH"
  fi
  export PATH="$dir:$PATH"
}

ci_export_env() {
  local key="${1:?key is required}"
  local value="${2-}"
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    printf '%s=%s\n' "$key" "$value" >>"$GITHUB_ENV"
  fi
  export "${key}=${value}"
}

ci_clear_env() {
  local key="${1:?key is required}"
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    printf '%s=\n' "$key" >>"$GITHUB_ENV"
  fi
  unset "$key" || true
}

ci_bootstrap_act_venv() {
  if [[ "${ACT:-}" != "true" || "${GITEA_ACTIONS:-}" == "true" ]]; then
    return 0
  fi
  python -m venv .venv
  ci_append_path "$PWD/.venv/bin"
  .venv/bin/python -m pip install --upgrade pip
}

ci_prepare_user_bin() {
  ci_append_path "$HOME/.local/bin"
}

ci_is_supported_actions_server() {
  if [[ "${ACT:-}" == "true" ]]; then
    return 0
  fi
  [[ "${GITHUB_SERVER_URL:-}" != "https://github.com" ]]
}

ci_require_supported_actions_server() {
  if ci_is_supported_actions_server; then
    return 0
  fi
  echo "This workflow is intended for Gitea Actions or local act only; refusing to run on ${GITHUB_SERVER_URL:-unknown}." >&2
  return 1
}

ci_maybe_trust_gitea() {
  if [[ "${ACT:-}" == "true" && "${GITEA_ACTIONS:-}" != "true" ]]; then
    return 0
  fi
  ci_require_supported_actions_server
  local host="${GITEA_HOST:-https://gitea.core.home.arpa/}"
  if [[ -n "${GITEA_CA_B64:-}" ]]; then
    local cert_path="${RUNNER_TEMP:-/tmp}/gitea-ca.crt"
    echo "$GITEA_CA_B64" | base64 -d >"$cert_path"
    git config --global "http.\"${host}\".sslVerify" true
    git config --global "http.\"${host}\".sslCAInfo" "$cert_path"
    ci_export_env GIT_SSL_CAINFO "$cert_path"
    ci_clear_env GIT_SSL_NO_VERIFY
    return 0
  fi
  git config --global "http.\"${host}\".sslVerify" false
  ci_export_env GIT_SSL_NO_VERIFY true
  ci_clear_env GIT_SSL_CAINFO
}

ci_install_project() {
  local editable_target="${1:-.[dev]}"
  python -m pip install --upgrade pip
  python -m pip install -e "$editable_target"
}

ci_configure_docker_env() {
  if [[ -S /var/run/docker.sock ]]; then
    ci_export_env DOCKER_HOST "unix:///var/run/docker.sock"
    ci_clear_env DOCKER_TLS_VERIFY
    ci_clear_env DOCKER_CERT_PATH
    ci_clear_env DOCKER_TLS_CERTDIR
    return 0
  fi
  if [[ -d /certs/client ]]; then
    ci_export_env DOCKER_HOST "tcp://docker:2376"
    ci_export_env DOCKER_TLS_VERIFY "1"
    ci_export_env DOCKER_CERT_PATH "/certs/client"
    ci_export_env DOCKER_TLS_CERTDIR "/certs"
  fi
}

ci_disable_docker_tls_verify() {
  # docker-py treats unset/empty DOCKER_TLS_VERIFY as verify=false.
  # A literal "0" still counts as enabled verification.
  if [[ -z "${DOCKER_HOST:-}" || "${DOCKER_HOST}" == unix://* ]]; then
    return 0
  fi
  if [[ -n "${DOCKER_CERT_PATH:-}" || -n "${DOCKER_TLS_CERTDIR:-}" ]]; then
    ci_clear_env DOCKER_TLS_VERIFY
  fi
}

ci_docker_published_host() {
  if [[ -n "${K1S_DOCKER_PUBLISHED_HOST:-}" ]]; then
    printf '%s\n' "${K1S_DOCKER_PUBLISHED_HOST}"
    return 0
  fi
  local docker_host="${DOCKER_HOST:-}"
  if [[ -z "$docker_host" || "$docker_host" == unix://* || "$docker_host" == npipe://* ]]; then
    printf '127.0.0.1\n'
    return 0
  fi
  docker_host="${docker_host#tcp://}"
  docker_host="${docker_host#http://}"
  docker_host="${docker_host#https://}"
  docker_host="${docker_host%%/*}"
  docker_host="${docker_host%%:*}"
  if [[ -z "$docker_host" ]]; then
    docker_host="127.0.0.1"
  fi
  printf '%s\n' "$docker_host"
}

ci_append_no_proxy() {
  local host="${1:?host is required}"
  local current="${NO_PROXY:-${no_proxy:-}}"
  local merged="$current"
  if [[ -z "$merged" ]]; then
    merged="$host"
  elif [[ ",${merged}," != *",${host},"* ]]; then
    merged="${merged},${host}"
  fi
  ci_export_env NO_PROXY "$merged"
  ci_export_env no_proxy "$merged"
}

ci_install_kubectl() {
  local version="${1:-v1.30.3}"
  ci_prepare_user_bin
  curl -fsSL "https://dl.k8s.io/release/${version}/bin/linux/amd64/kubectl" -o "$HOME/.local/bin/kubectl"
  chmod +x "$HOME/.local/bin/kubectl"
}

ci_install_helm() {
  local version="${1:-v3.14.4}"
  local tarball="/tmp/helm.tgz"
  ci_prepare_user_bin
  rm -f "$tarball"
  for url in \
    "https://get.helm.sh/helm-${version}-linux-amd64.tar.gz" \
    "https://storage.googleapis.com/kubernetes-helm/helm-${version}-linux-amd64.tar.gz"; do
    if curl -fL --retry 5 --retry-delay 2 --retry-connrefused "$url" -o "$tarball"; then
      break
    fi
  done
  if [[ ! -s "$tarball" ]]; then
    echo "failed to download helm ${version}" >&2
    return 1
  fi
  rm -rf /tmp/linux-amd64
  tar -xzf "$tarball" -C /tmp
  mv /tmp/linux-amd64/helm "$HOME/.local/bin/helm"
  chmod +x "$HOME/.local/bin/helm"
}

ci_install_kind() {
  local version="${1:-v0.23.0}"
  ci_prepare_user_bin
  curl -fsSL "https://kind.sigs.k8s.io/dl/${version}/kind-linux-amd64" -o "$HOME/.local/bin/kind"
  chmod +x "$HOME/.local/bin/kind"
}

ci_install_k3d() {
  ci_prepare_user_bin
  curl -fsSL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | USE_SUDO=false K3D_INSTALL_DIR="$HOME/.local/bin" bash
}

ci_install_podman() {
  sudo apt-get update
  sudo apt-get install -y podman
  podman --version
}

ci_install_age_sops() {
  sudo apt-get update
  sudo apt-get install -y age sops
}
