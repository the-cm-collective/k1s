"""Tests for the secret manager."""

from pathlib import Path

import pytest

from ae.controller.spec import SecretEnvMapping, SecretRef
from ae.secrets import SecretManager


def write_secret(path: Path, content: str) -> None:
    path.write_text(content)


def test_secret_manager_plaintext(tmp_path, monkeypatch):
    secret_path = tmp_path / "secret.yaml"
    write_secret(
        secret_path,
        """
FOO: bar
BAZ: qux
        """.strip(),
    )

    monkeypatch.setenv("AE_ALLOW_PLAINTEXT_SECRETS", "1")
    manager = SecretManager()
    env = manager.load_env(
        [
            SecretRef(
                name="demo",
                path=str(secret_path),
                env=[
                    SecretEnvMapping(name="FOO_ENV", key="FOO"),
                    SecretEnvMapping(name="BAZ_ENV", key="BAZ"),
                ],
            )
        ]
    )

    assert env == {"FOO_ENV": "bar", "BAZ_ENV": "qux"}


def test_secret_manager_missing_key(tmp_path, monkeypatch):
    secret_path = tmp_path / "secret.json"
    write_secret(secret_path, '{"FOO": "bar"}')

    monkeypatch.setenv("AE_ALLOW_PLAINTEXT_SECRETS", "1")
    manager = SecretManager()

    with pytest.raises(KeyError):
        manager.load_env(
            [
                SecretRef(
                    name="demo",
                    path=str(secret_path),
                    env=[SecretEnvMapping(name="BAR_ENV", key="BAR")],
                )
            ]
        )
