from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AE_ENV_SCRIPT = ROOT / "scripts" / "ae-env.sh"
ENSURE_CONTROLLER_ENV_SCRIPT = ROOT / "scripts" / "ensure_controller_env.sh"


def _read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def test_ensure_controller_env_aligns_admin_and_preserves_existing_tokens(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-ha-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    apishim_env = profile_dir / "apishim.env"
    controller_env = profile_dir / "controller.env"

    apishim_env.write_text(
        "\n".join(
            [
                "AE_API_ADMIN_TOKEN=ha-admin-token-0123456789abcdef",
                "AE_LABS_TOKEN=ha-labs-token-0123456789abcdef",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    controller_env.write_text(
        "\n".join(
            [
                "AE_API_ADMIN_TOKEN=stale-admin-token",
                "AE_API_SCALER_TOKEN=ha-scaler-token-0123456789abcdef",
                "AE_API_READ_TOKEN=ha-read-token-0123456789abcdef",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "APISHIM_ENV_FILE": str(apishim_env),
            "CONTROLLER_ENV_FILE": str(controller_env),
            "AE_STATE_DB": str(profile_dir / "controller.db"),
            "AE_STATE_BACKEND": "etcd",
            "AE_ETCD_ENDPOINTS": "http://127.0.0.1:2379",
            "AE_ETCD_PREFIX": "k1s/profiles/k1s-ha-core",
            "AE_APISHIM_SERVER": "https://127.0.0.1:8445",
        }
    )
    subprocess.run(
        [str(ENSURE_CONTROLLER_ENV_SCRIPT)],
        check=True,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    payload = _read_env_file(controller_env)
    assert payload["AE_API_ADMIN_TOKEN"] == "ha-admin-token-0123456789abcdef"
    assert payload["AE_API_SCALER_TOKEN"] == "ha-scaler-token-0123456789abcdef"
    assert payload["AE_API_READ_TOKEN"] == "ha-read-token-0123456789abcdef"
    assert payload["AE_LABS_TOKEN"] == "ha-labs-token-0123456789abcdef"
    assert payload["AE_STATE_DB"] == str(profile_dir / "controller.db")
    assert payload["AE_STATE_BACKEND"] == "etcd"
    assert payload["AE_ETCD_ENDPOINTS"] == "http://127.0.0.1:2379"
    assert payload["AE_ETCD_PREFIX"] == "k1s/profiles/k1s-ha-core"
    assert payload["AE_APISHIM_SERVER"] == "https://127.0.0.1:8445"


def test_ae_env_local_prefers_sibling_profile_controller_env(tmp_path: Path) -> None:
    profile_dir = tmp_path / "state" / "profiles" / "k1s-ha-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    apishim_env = profile_dir / "apishim.env"
    controller_env = profile_dir / "controller.env"
    dev_env = tmp_path / "state" / "dev.env"
    dev_env.parent.mkdir(parents=True, exist_ok=True)

    apishim_env.write_text(
        "AE_API_ADMIN_TOKEN=ha-admin-token\n",
        encoding="utf-8",
    )
    controller_env.write_text(
        "\n".join(
            [
                "AE_API_SCALER_TOKEN=ha-scaler-token",
                "AE_API_READ_TOKEN=ha-read-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    dev_env.write_text("", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "APISHIM_ENV_FILE": str(apishim_env),
            "DEV_ENV_FILE": str(dev_env),
        }
    )
    res = subprocess.run(
        [str(AE_ENV_SCRIPT), "local"],
        check=True,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert "export AE_API_ADMIN_TOKEN=ha-admin-token" in res.stdout
    assert "export AE_API_SCALER_TOKEN=ha-scaler-token" in res.stdout
    assert "export AE_API_READ_TOKEN=ha-read-token" in res.stdout
