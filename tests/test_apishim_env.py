from pathlib import Path

from ae.apishim.env import ensure_local_apishim_env


def test_ensure_local_apishim_env_writes_tokens_without_openssl(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "")
    env_file = tmp_path / "apishim.env"
    result = ensure_local_apishim_env(
        env_file=env_file,
        cert_file=tmp_path / "apishim.crt",
        key_file=tmp_path / "apishim.key",
        ca_file=tmp_path / "apishim.ca.crt",
        ca_key_file=tmp_path / "apishim.ca.key",
        environ={},
    )

    text = env_file.read_text(encoding="utf-8")
    assert "AE_APISHIM_TOKEN=" in text
    assert "AE_APISHIM_READ_TOKEN=" in text
    assert "AE_APISHIM_SESSION_SECRET=" in text
    assert result["APISHIM_ENV_FILE"] == str(env_file)
    assert env_file.stat().st_mode & 0o777 == 0o600
