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
ENSURE_DEV_ENV = ROOT / "scripts" / "ensure_dev_env.sh"
INIT_DEMO = ROOT / "scripts" / "init_demo.sh"
NIXOS_BRIDGE = ROOT / "ops" / "nixos" / "k1s-local-dev-bridge.nix"
DOCKER_COMPOSE = ROOT / "ops" / "dev" / "docker-compose.yaml"


def test_flake_declares_default_and_cri_shells() -> None:
    text = FLAKE.read_text(encoding="utf-8")
    assert 'description = "k1s additive development shells";' in text
    assert "default = pkgs.mkShell" in text
    assert "cri = pkgs.mkShell" in text
    assert "podman-compose" in text
    assert 'python -m venv .venv && . .venv/bin/activate && python -m pip install -e .[dev]' in text
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
    assert 'export AE_APISHIM_ETCD_ENDPOINTS="${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}"' in text
    assert 'export APISHIM_CONTAINER_PORT="${APISHIM_CONTAINER_PORT:-8445}"' in text
    assert 'dev_local_hosts="blue.home.arpa green.home.arpa' in text
    assert 'DEV_LOCAL_HOSTS="$dev_local_hosts"' in text


def test_compose_and_dev_env_use_explicit_podman_safe_apishim_values() -> None:
    compose_text = DOCKER_COMPOSE.read_text(encoding="utf-8")
    dev_env_text = ENSURE_DEV_ENV.read_text(encoding="utf-8")
    assert '${AE_APISHIM_ETCD_ENDPOINTS:-${AE_ETCD_ENDPOINTS:-}}' not in compose_text
    assert '${APISHIM_HOST_PORT:-${APISHIM_PORT:-8445}}' not in compose_text
    assert 'AE_ETCD_ENDPOINTS=${AE_APISHIM_ETCD_ENDPOINTS:-}' in compose_text
    assert '"127.0.0.1:${APISHIM_HOST_PORT:-8445}:8445"' in compose_text
    assert 'apishim_container_port="${APISHIM_CONTAINER_PORT:-8445}"' in dev_env_text
    assert 'apishim_upstream="apishim:${apishim_container_port}"' in dev_env_text
    assert "printf 'APISHIM_CONTAINER_PORT=%s\\n'" in dev_env_text


def test_ensure_dev_local_supports_nixos_bridge_and_real_ca_sources() -> None:
    text = ENSURE_DEV_LOCAL.read_text(encoding="utf-8")
    assert "/var/lib/k1s-dev" in text
    assert "nixos-rebuild switch --impure" in text
    assert "AE_NIXOS_REBUILD" in text
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
