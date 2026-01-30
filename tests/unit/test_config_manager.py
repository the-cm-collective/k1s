"""Tests for the config manager."""

from pathlib import Path

import pytest

from ae.config.manager import ConfigManager
from ae.controller.spec import ConfigEnvMapping, ConfigRef


def write_config(path: Path, content: str) -> None:
    path.write_text(content)


def test_config_manager_env_from(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        """
FOO: bar
BAZ: qux
        """.strip(),
    )

    manager = ConfigManager()
    env = manager.load_env([ConfigRef(name="demo", path=str(cfg_path), env_from=True)])
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_config_manager_env_from_allows_mapping_override(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        """
FOO: bar
ALT: qux
        """.strip(),
    )

    manager = ConfigManager()
    env = manager.load_env(
        [
            ConfigRef(
                name="demo",
                path=str(cfg_path),
                env_from=True,
                env=[ConfigEnvMapping(name="FOO", key="ALT")],
            )
        ]
    )
    assert env["FOO"] == "qux"
    assert env["ALT"] == "qux"


def test_config_manager_missing_key(tmp_path):
    cfg_path = tmp_path / "config.json"
    write_config(cfg_path, '{"FOO": "bar"}')

    manager = ConfigManager()
    with pytest.raises(KeyError):
        manager.load_env(
            [
                ConfigRef(
                    name="demo",
                    path=str(cfg_path),
                    env=[ConfigEnvMapping(name="BAR_ENV", key="BAR")],
                )
            ]
        )
