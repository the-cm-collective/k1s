from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install.sh"


def test_ha_core_install_and_uninstall_use_separate_surface(tmp_path: Path) -> None:
    systemd_dir = tmp_path / "systemd"
    etc_dir = tmp_path / "etc"
    bin_dir = tmp_path / "bin"
    env = {
        **os.environ,
        "PREFIX_SYSTEMD": str(systemd_dir),
        "PREFIX_ETC": str(etc_dir),
        "PREFIX_BIN": str(bin_dir),
    }

    subprocess.run(
        ["bash", str(SCRIPT), "ha-core-install"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    unit_path = systemd_dir / "ae-ha-core.service"
    env_path = etc_dir / "ha-core.env"
    wrapper_path = bin_dir / "ae-ha-core-service"
    legacy_unit = systemd_dir / "ae-controller.service"

    assert unit_path.exists()
    assert env_path.exists()
    assert wrapper_path.exists()
    assert not legacy_unit.exists()

    unit_text = unit_path.read_text(encoding="utf-8")
    assert f"EnvironmentFile={env_path}" in unit_text
    assert f"ExecStart={wrapper_path}" in unit_text

    env_text = env_path.read_text(encoding="utf-8")
    assert f"AE_REPO_ROOT={ROOT}" in env_text

    wrapper_text = wrapper_path.read_text(encoding="utf-8")
    assert "run_profile.sh" in wrapper_text
    assert "k1s-ha-core" in wrapper_text

    subprocess.run(
        ["bash", str(SCRIPT), "ha-core-uninstall"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    assert not unit_path.exists()
    assert not wrapper_path.exists()
    assert env_path.exists()
