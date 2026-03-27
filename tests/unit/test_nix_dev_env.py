from __future__ import annotations

# ruff: noqa: S603
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLAKE = ROOT / "flake.nix"
ENVRC = ROOT / ".envrc"
GITIGNORE = ROOT / ".gitignore"
MAKEFILE = ROOT / "Makefile"
RUN_PROFILE = ROOT / "scripts" / "dev" / "run_profile.sh"
ENV_DOCTOR = ROOT / "scripts" / "dev" / "env_doctor.sh"
ENSURE_DEV_LOCAL = ROOT / "scripts" / "dev" / "ensure_dev_local.sh"
NIXOS_BRIDGE_HELPER = ROOT / "scripts" / "lib" / "nixos_bridge.sh"
ENSURE_DEV_ENV = ROOT / "scripts" / "ensure_dev_env.sh"
INIT_DEMO = ROOT / "scripts" / "init_demo.sh"
NIXOS_BRIDGE = ROOT / "ops" / "nixos" / "k1s-local-dev-bridge.nix"
NIXOS_CRI_HOST = ROOT / "ops" / "nixos" / "k1s-cri-host.nix"
DOCKER_COMPOSE = ROOT / "ops" / "dev" / "docker-compose.yaml"


def _case_body(text: str, label: str, next_label: str) -> str:
    start = text.index(f"  {label})")
    end = text.index(f"  {next_label})")
    return text[start:end]


def test_flake_declares_default_and_cri_shells() -> None:
    text = FLAKE.read_text(encoding="utf-8")
    assert 'description = "k1s additive development shells";' in text
    assert "default = pkgs.mkShell" in text
    assert "cri = pkgs.mkShell" in text
    assert "podman-compose" in text
    assert "python -m venv .venv && . .venv/bin/activate && python -m pip install -e .[dev]" in text
    assert "PODMAN_COMPOSE_PROVIDER" in text


def test_envrc_uses_flake_and_gitignore_skips_direnv() -> None:
    assert ENVRC.read_text(encoding="utf-8").strip() == "use flake"
    assert ".direnv/" in GITIGNORE.read_text(encoding="utf-8")


def test_makefile_exposes_env_doctor_target() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "env-doctor" in text
    assert "@./scripts/dev/env_doctor.sh" in text
    assert "dev-local-clean" in text
    assert "@AE_DEV_LOCAL_ACTION=clean ./scripts/dev/ensure_dev_local.sh" in text


def test_run_profile_guards_compose_provider_and_host_fallback() -> None:
    text = RUN_PROFILE.read_text(encoding="utf-8")
    assert "compose_provider_available()" in text
    assert "compose_provider_hint()" in text
    assert "default_apishim_mode()" in text
    assert "prepare_apishim_compose_env()" in text
    assert "apishim_compose_render()" in text
    assert "up_apishim_compose()" in text
    assert "defaulting AE_APISHIM_MODE=host" in text
    assert "AE_APISHIM_MODE=container requires a working" in text
    assert (
        'export AE_APISHIM_ETCD_ENDPOINTS="${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}"'
        in text
    )
    assert 'export APISHIM_CONTAINER_PORT="${APISHIM_CONTAINER_PORT:-8445}"' in text
    assert 'dev_local_hosts="blue.home.arpa green.home.arpa' in text
    assert 'DEV_LOCAL_HOSTS="$dev_local_hosts"' in text
    assert "defaulting AE_CRI_IMAGE_MIRROR_BACKEND=podman on NixOS for managed registry TLS" in text
    assert "defaulting AE_CRI_IMAGE_BUILD_BACKEND=podman on NixOS for local image builds" in text
    assert "default_profile_cri_data_root()" in text
    assert "defaulting AE_CRI_DATA_ROOT=" in text
    assert "ensure_default_local_apishim_postgres_auth()" in text
    assert "ensure_strict_cri_ingress_ready()" in text
    assert "profile_state_ownership.sh" in text
    assert "ensure_strict_cri_profile_state_ownership" in text
    assert "STRICT_CRI_OWNERSHIP_REPAIR_PROFILE" in text
    assert "STRICT_CRI_OWNERSHIP_HELPER_ARGS=()" in text
    assert "strict_cri_explicit_target_configured()" in text
    assert "AE_STRICT_CRI_TARGET_UID and AE_STRICT_CRI_TARGET_GID must be set together." in text
    assert "render_core_proxy_bootstrap_from_env" in text
    assert 'bootstrap-state.db' in text
    assert '--network host --user 0 \\' in text
    assert 'envoy-base-id --profile "$PROFILE" --component "$name"' in text
    assert '--base-id "$base_id"' in text
    assert "strict CRI ingress listener not ready" in text
    assert "strict CRI profile state under state/profiles/" in text
    assert "prior sudo -E strict CRI run" in text
    assert "run_cri_stack up-postgres --profile \"$PROFILE\" --reset-data --recreate" in text
    assert "strict CRI on NixOS requires the k1s CRI host module" in text
    assert 'k1s_nixos_cri_bootstrap_instructions "$ROOT_DIR" "$cri_module_dest"' in text
    assert 'resolved_cni_env="$(k1s_containerd_cni_env || true)"' in text
    assert "[cri] using NixOS containerd-managed CNI paths" in text
    assert 'local bootstrap_script="$ROOT_DIR/scripts/cni_bin_bootstrap.sh"' in text
    assert 'bash "$bootstrap_script"' in text
    assert "-print -quit | grep -q ." in text
    assert "strict CRI infra selected but CNI plugin bootstrap failed" in text
    assert "containerd_socket_access.sh --grant" in text
    assert "run_controller_loop --loop" in text


def test_compose_and_dev_env_use_explicit_podman_safe_apishim_values() -> None:
    compose_text = DOCKER_COMPOSE.read_text(encoding="utf-8")
    dev_env_text = ENSURE_DEV_ENV.read_text(encoding="utf-8")
    assert "${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}" not in compose_text
    assert "${APISHIM_HOST_PORT:-${APISHIM_PORT:-8445}}" not in compose_text
    assert "AE_ETCD_ENDPOINTS=${AE_APISHIM_ETCD_ENDPOINTS:-}" in compose_text
    assert '"127.0.0.1:${APISHIM_HOST_PORT:-8445}:8445"' in compose_text
    assert 'apishim_container_port="${APISHIM_CONTAINER_PORT:-8445}"' in dev_env_text
    assert 'apishim_upstream="apishim:${apishim_container_port}"' in dev_env_text
    assert "printf 'APISHIM_CONTAINER_PORT=%s\\n'" in dev_env_text


def test_run_profile_defaults_docs_on_for_public_controlplane_ingress() -> None:
    text = RUN_PROFILE.read_text(encoding="utf-8")
    core_body = _case_body(text, "k1s-core", "k1s-ha-core")
    ha_body = _case_body(text, "k1s-ha-core", "k1s-edge")
    guard = (
        'if is_truthy "${AE_CONTROLPLANE_PUBLIC_ENABLE:-0}"; then\n'
        "      # Public control-plane ingress expects the docs upstream to be live.\n"
        '      export CORE_DOCS="${CORE_DOCS:-1}"\n'
        "    fi"
    )
    assert guard in core_body
    assert guard in ha_body
    assert 'export CORE_DOCS="${CORE_DOCS:-0}"' not in ha_body


def test_run_profile_syncs_profile_local_controller_env_for_core_profiles() -> None:
    text = RUN_PROFILE.read_text(encoding="utf-8")
    core_body = _case_body(text, "k1s-core", "k1s-ha-core")
    ha_body = _case_body(text, "k1s-ha-core", "k1s-edge")
    assert "sync_controller_env() {" in text
    assert '"$ROOT_DIR/scripts/ensure_controller_env.sh"' in text
    assert 'local controller_env_file="${CONTROLLER_ENV_FILE:-$profile_dir/controller.env}"' in text
    assert 'local apishim_env_file="${APISHIM_ENV_FILE:-$profile_dir/apishim.env}"' in text
    assert 'sync_controller_env "$PROFILE_DIR"' in core_body
    assert 'sync_controller_env "$PROFILE_DIR"' in ha_body


def test_ensure_dev_local_supports_nixos_bridge_and_real_ca_sources() -> None:
    text = ENSURE_DEV_LOCAL.read_text(encoding="utf-8")
    helper_text = NIXOS_BRIDGE_HELPER.read_text(encoding="utf-8")
    assert 'source "${ROOT_DIR}/scripts/lib/nixos_bridge.sh"' in text
    assert "/var/lib/k1s-dev" in helper_text
    assert "nixos-rebuild switch --impure" in helper_text
    assert "AE_NIXOS_REBUILD" in helper_text
    assert "AE_NIXOS_CRI_MODULE_DEST" in helper_text
    assert "k1s_nixos_cri_module_imported" in helper_text
    assert "k1s_containerd_cni_env" in helper_text
    assert "apishim.ca.crt" in text
    assert "update-ca-trust" in text
    assert "blue.home.arpa" in text
    assert "green.home.arpa" in text


def test_nixos_bridge_module_reads_hosts_and_certs_from_bridge_root() -> None:
    text = NIXOS_BRIDGE.read_text(encoding="utf-8")
    assert "/var/lib/k1s-dev" in text
    assert "networking.extraHosts" in text
    assert "security.pki.certificateFiles" in text
    assert "builtins.readFile" in text
    assert "builtins.readDir" in text


def test_nixos_cri_module_declares_containerd_and_repo_cni_contract() -> None:
    text = NIXOS_CRI_HOST.read_text(encoding="utf-8")
    assert "virtualisation.containerd" in text
    assert 'registry.config_path = "/etc/containerd/certs.d";' in text
    assert "${pkgs.cni-plugins}/bin" in text
    assert '"cniVersion": "0.4.0"' in text
    assert '"subnet": "10.88.0.0/16"' in text
    assert "cri-tools" in text


def test_init_demo_delegates_local_dns_tls_helper() -> None:
    text = INIT_DEMO.read_text(encoding="utf-8")
    assert "Apply local DNS/TLS helper state" in text
    assert "Remove local DNS/TLS helper state" in text
    assert "./scripts/dev/ensure_dev_local.sh" in text
    assert "AE_APISHIM_TLS_CA_CERT" in text


def test_env_doctor_runs_and_reports_core_sections() -> None:
    res = subprocess.run(
        [str(ENV_DOCTOR)],
        text=True,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr
    assert "[env-doctor] toolchain" in res.stdout
    assert "podman compose" in res.stdout
    assert "podman apishim render" in res.stdout
    assert "containerd socket" in res.stdout
    assert "[env-doctor] local dns / trust" in res.stdout
    assert "combined dev CA" in res.stdout
    assert "docs.home.arpa" in res.stdout
    assert "default shell" in res.stdout
