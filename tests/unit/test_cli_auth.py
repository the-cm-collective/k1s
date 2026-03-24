import os

import pytest

from ae.cli import __main__ as cli


AUTH_ENV_KEYS = (
    "AE_APISHIM_TOKEN",
    "AE_APISHIM_READ_TOKEN",
    "AE_APISHIM_EXEC_TOKEN",
    "AE_APISHIM_PORTFORWARD_TOKEN",
    "AE_APISHIM_SESSION_SECRET",
    "AE_APISHIM_MINT_TOKEN",
    "AE_LABS_TOKEN",
    "AE_APISHIM_CA_BUNDLE",
    "AE_APISHIM_CA",
    "AE_APISHIM_TLS_CA",
    "AE_APISHIM_SERVER",
    "APISHIM_ENV_FILE",
    "AE_APISHIM_DB",
    "AE_STATE_DB",
    "CONTROLLER_ENV_FILE",
    "DEV_ENV_FILE",
    "APISHIM_PID_FILE",
)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    for key in AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_auth_local_strict_fails_without_stream_tokens(tmp_path, capsys):
    apishim_env = tmp_path / "apishim.env"
    controller_env = tmp_path / "controller.env"
    dev_env = tmp_path / "dev.env"
    pid_path = tmp_path / "apishim.pid"
    apishim_env.write_text("AE_APISHIM_SERVER=https://127.0.0.1:8445\n", encoding="utf-8")
    controller_env.write_text("", encoding="utf-8")
    dev_env.write_text("", encoding="utf-8")
    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--apishim-env",
            str(apishim_env),
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "strict mode requires" in err


def test_auth_local_exports_mint_token(tmp_path, capsys):
    apishim_env = tmp_path / "apishim.env"
    controller_env = tmp_path / "controller.env"
    dev_env = tmp_path / "dev.env"
    pid_path = tmp_path / "apishim.pid"
    apishim_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_MINT_TOKEN=mint-demo-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    controller_env.write_text("", encoding="utf-8")
    dev_env.write_text("", encoding="utf-8")
    rc = cli.main(
        [
            "auth",
            "local",
            "--apishim-env",
            str(apishim_env),
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "export AE_APISHIM_MINT_TOKEN=mint-demo-token" in out
    assert "export AE_APISHIM_SERVER=https://127.0.0.1:8445" in out


def test_auth_local_exports_ca_bundle(tmp_path, capsys):
    apishim_env = tmp_path / "apishim.env"
    controller_env = tmp_path / "controller.env"
    dev_env = tmp_path / "dev.env"
    pid_path = tmp_path / "apishim.pid"
    ca_bundle = tmp_path / "apishim.ca.crt"
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    apishim_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_MINT_TOKEN=mint-demo-token",
                f"AE_APISHIM_CA_BUNDLE={ca_bundle}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    controller_env.write_text("", encoding="utf-8")
    dev_env.write_text("", encoding="utf-8")
    rc = cli.main(
        [
            "auth",
            "local",
            "--apishim-env",
            str(apishim_env),
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"export AE_APISHIM_CA_BUNDLE={ca_bundle}" in out


def test_auth_local_strict_allows_labs_token_only(tmp_path, capsys):
    apishim_env = tmp_path / "apishim.env"
    controller_env = tmp_path / "controller.env"
    dev_env = tmp_path / "dev.env"
    pid_path = tmp_path / "apishim.pid"
    apishim_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_LABS_TOKEN=labs-demo-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    controller_env.write_text("", encoding="utf-8")
    dev_env.write_text("", encoding="utf-8")
    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--apishim-env",
            str(apishim_env),
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "export AE_LABS_TOKEN=labs-demo-token" in out


def test_auth_mint_emits_token(monkeypatch, capsys):
    monkeypatch.setenv("AE_APISHIM_SERVER", "https://127.0.0.1:8445")
    monkeypatch.setenv("AE_APISHIM_MINT_TOKEN", "mint-demo-token")
    monkeypatch.setattr(
        cli,
        "_mint_apishim_session_token",
        lambda *_a, **_k: {"token": "sess1.demo.sig", "expires_at": 9999999999},
    )
    rc = cli.main(["auth", "mint", "--role", "exec", "--scope", "default/demo"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "sess1.demo.sig"


def test_auth_local_strict_prefers_inferred_shared_cli_env(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    root_env = profile_dir / "apishim.env"
    cli_env = profile_dir / "apishim.cli.env"
    controller_env = tmp_path / "state" / "env.sh"
    dev_env = tmp_path / "state" / "dev.env"
    pid_path = tmp_path / "state" / "apishim.pid"

    root_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_TOKEN=root-admin-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cli_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_MINT_TOKEN=shared-mint-token",
                "AE_APISHIM_CA_BUNDLE=state/profiles/k1s-core/apishim.ca.crt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Simulate root-owned root env: unreadable for the current user shell.
    os.chmod(root_env, 0)
    controller_env.parent.mkdir(parents=True, exist_ok=True)
    controller_env.write_text(
        "AE_STATE_DB=state/profiles/k1s-core/controller.db\n",
        encoding="utf-8",
    )
    dev_env.write_text("", encoding="utf-8")

    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "export AE_APISHIM_MINT_TOKEN=shared-mint-token" in out
    assert "export AE_APISHIM_CA_BUNDLE=state/profiles/k1s-core/apishim.ca.crt" in out


def test_auth_local_infers_controller_http_tokens_from_sibling_profile_env(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    profile_dir = tmp_path / "state" / "profiles" / "k1s-ha-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    root_env = profile_dir / "apishim.env"
    cli_env = profile_dir / "apishim.cli.env"
    controller_env = tmp_path / "state" / "env.sh"
    dev_env = tmp_path / "state" / "dev.env"
    pid_path = tmp_path / "state" / "apishim.pid"

    root_env.write_text(
        "\n".join(
            [
                "AE_API_ADMIN_TOKEN=ha-admin-token",
                "AE_LABS_TOKEN=ha-labs-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cli_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_MINT_TOKEN=shared-mint-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    controller_env.parent.mkdir(parents=True, exist_ok=True)
    controller_env.write_text(
        "AE_STATE_DB=state/profiles/k1s-ha-core/controller.db\n",
        encoding="utf-8",
    )
    dev_env.write_text("", encoding="utf-8")

    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "export AE_APISHIM_MINT_TOKEN=shared-mint-token" in out
    assert "export AE_API_ADMIN_TOKEN=ha-admin-token" in out
    assert "export AE_LABS_TOKEN=ha-labs-token" in out


def test_auth_local_strict_hints_shared_env_for_unreadable_profile_env(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    root_env = profile_dir / "apishim.env"
    controller_env = tmp_path / "state" / "env.sh"
    dev_env = tmp_path / "state" / "dev.env"
    pid_path = tmp_path / "state" / "apishim.pid"

    root_env.write_text("AE_APISHIM_SERVER=https://127.0.0.1:8445\n", encoding="utf-8")
    os.chmod(root_env, 0)
    controller_env.parent.mkdir(parents=True, exist_ok=True)
    controller_env.write_text(
        "AE_STATE_DB=state/profiles/k1s-core/controller.db\n",
        encoding="utf-8",
    )
    dev_env.write_text("", encoding="utf-8")

    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "strict mode requires" in err
    assert "apishim.cli.env" in err


def test_auth_local_infers_profile_ca_bundle_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    profile_dir = tmp_path / "state" / "profiles" / "k1s-core"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cli_env = profile_dir / "apishim.cli.env"
    ca_bundle = profile_dir / "apishim.ca.crt"
    controller_env = tmp_path / "state" / "env.sh"
    dev_env = tmp_path / "state" / "dev.env"
    pid_path = tmp_path / "state" / "apishim.pid"

    cli_env.write_text(
        "\n".join(
            [
                "AE_APISHIM_SERVER=https://127.0.0.1:8445",
                "AE_APISHIM_MINT_TOKEN=shared-mint-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ca_bundle.write_text("dummy-ca", encoding="utf-8")
    controller_env.parent.mkdir(parents=True, exist_ok=True)
    controller_env.write_text(
        "AE_STATE_DB=state/profiles/k1s-core/controller.db\n",
        encoding="utf-8",
    )
    dev_env.write_text("", encoding="utf-8")

    rc = cli.main(
        [
            "auth",
            "local",
            "--strict",
            "--controller-env",
            str(controller_env),
            "--dev-env",
            str(dev_env),
            "--apishim-pid",
            str(pid_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"export AE_APISHIM_CA_BUNDLE={ca_bundle}" in out
