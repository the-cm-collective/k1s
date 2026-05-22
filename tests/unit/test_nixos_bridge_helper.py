from __future__ import annotations

# ruff: noqa: S603
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "lib" / "nixos_bridge.sh"
REGISTRY_TRUST = ROOT / "scripts" / "containerd_registry_trust.sh"


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        text=True,
        capture_output=True,
        check=False,
    )


def test_nixos_bridge_helper_detects_imported_module(tmp_path) -> None:
    nixos_root = tmp_path / "etc" / "nixos"
    module_dest = nixos_root / "nixos" / "modules" / "k1s-local-dev-bridge.nix"
    module_dest.parent.mkdir(parents=True, exist_ok=True)
    module_dest.write_text("{ ... }: {}\n", encoding="utf-8")
    (nixos_root / "configuration.nix").write_text(
        "imports = [ ./nixos/modules/k1s-local-dev-bridge.nix ];\n",
        encoding="utf-8",
    )

    res = _bash(
        f'source "{HELPER}"; '
        f'if k1s_nixos_bridge_imported "{nixos_root}" "{module_dest}"; then '
        "echo imported; else echo missing; fi"
    )

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "imported"


def test_nixos_bridge_helper_ignores_unimported_module(tmp_path) -> None:
    nixos_root = tmp_path / "etc" / "nixos"
    module_dest = nixos_root / "nixos" / "modules" / "k1s-local-dev-bridge.nix"
    module_dest.parent.mkdir(parents=True, exist_ok=True)
    module_dest.write_text("{ ... }: {}\n", encoding="utf-8")

    res = _bash(
        f'source "{HELPER}"; '
        f'if k1s_nixos_bridge_imported "{nixos_root}" "{module_dest}"; then '
        "echo imported; else echo missing; fi"
    )

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "missing"


def test_nixos_bridge_helper_detects_imported_cri_module(tmp_path) -> None:
    nixos_root = tmp_path / "etc" / "nixos"
    module_dest = nixos_root / "nixos" / "modules" / "k1s-cri-host.nix"
    module_dest.parent.mkdir(parents=True, exist_ok=True)
    module_dest.write_text("{ ... }: {}\n", encoding="utf-8")
    (nixos_root / "configuration.nix").write_text(
        "imports = [ ./nixos/modules/k1s-cri-host.nix ];\n",
        encoding="utf-8",
    )

    res = _bash(
        f'source "{HELPER}"; '
        f'if k1s_nixos_cri_module_imported "{nixos_root}" "{module_dest}"; then '
        "echo imported; else echo missing; fi"
    )

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "imported"


def test_nixos_bridge_helper_reports_containerd_cni_env(tmp_path) -> None:
    fake_containerd = tmp_path / "containerd"
    fake_containerd.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "config" && "${2:-}" == "dump" ]]; then
  cat <<'EOF'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.cni]
  bin_dirs = ['/nix/store/unit-cni/bin']
  conf_dir = '/etc/cni/net.d'
EOF
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_containerd.chmod(0o755)

    res = _bash(f'source "{HELPER}"; CONTAINERD_BIN="{fake_containerd}" k1s_containerd_cni_env')

    assert res.returncode == 0, res.stderr
    assert "export CNI_BIN_DIR=/nix/store/unit-cni/bin" in res.stdout
    assert "export CNI_CONF_DIR=/etc/cni/net.d" in res.stdout


def test_nixos_bridge_helper_cri_bootstrap_instructions_reference_repo_module() -> None:
    res = _bash(f'source "{HELPER}"; k1s_nixos_cri_bootstrap_instructions "{ROOT}"')

    assert res.returncode == 0, res.stderr
    assert "ops/nixos/k1s-cri-host.nix" in res.stdout
    assert "./nixos/modules/k1s-cri-host.nix" in res.stdout


def test_containerd_registry_trust_help_mentions_nixos_bridge() -> None:
    res = subprocess.run(
        [str(REGISTRY_TRUST), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert res.returncode == 0, res.stderr
    assert "Linux/NixOS bridge" in res.stdout
    assert "--podman-root" in res.stdout
    assert "--docker" in res.stdout


def test_containerd_registry_trust_writes_backend_ca_paths(tmp_path) -> None:
    ca = tmp_path / "registry-ca.crt"
    ca.write_text("unit-ca\n", encoding="utf-8")
    containerd_root = tmp_path / "containerd-certs"
    podman_root = tmp_path / "podman-certs"
    docker_root = tmp_path / "docker-certs"
    podman_home = tmp_path / "home" / "ae"

    env = os.environ.copy()
    env["K1S_REGISTRY_TRUST_ALLOW_UNPRIVILEGED"] = "1"
    env["K1S_CONTAINERD_CERTS_DIR_ROOT"] = str(containerd_root)
    env["K1S_PODMAN_CERTS_DIR_ROOT"] = str(podman_root)
    env["K1S_DOCKER_CERTS_DIR_ROOT"] = str(docker_root)
    env["K1S_CONTAINERD_CONFIG_FILE"] = str(tmp_path / "missing-config.toml")

    res = subprocess.run(
        [
            str(REGISTRY_TRUST),
            "--host",
            "localhost:5001",
            "--ca",
            str(ca),
            "--podman-root",
            "--podman-user-home",
            str(podman_home),
            "--docker",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert (containerd_root / "localhost:5001" / "ca.crt").read_text(encoding="utf-8") == "unit-ca\n"
    assert (podman_root / "localhost:5001" / "ca.crt").read_text(encoding="utf-8") == "unit-ca\n"
    assert (
        podman_home / ".config" / "containers" / "certs.d" / "localhost:5001" / "ca.crt"
    ).read_text(encoding="utf-8") == "unit-ca\n"
    assert (docker_root / "localhost:5001" / "ca.crt").read_text(encoding="utf-8") == "unit-ca\n"
    hosts_toml = (containerd_root / "localhost:5001" / "hosts.toml").read_text(encoding="utf-8")
    assert f'ca = "{containerd_root}/localhost:5001/ca.crt"' in hosts_toml
