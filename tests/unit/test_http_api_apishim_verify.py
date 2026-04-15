from __future__ import annotations

import pytest

from ae.observability import http_api


@pytest.fixture(autouse=True)
def _clear_apishim_verify_env(monkeypatch, tmp_path):
    for key in (
        "AE_APISHIM_CA_BUNDLE",
        "AE_APISHIM_CA",
        "AE_APISHIM_TLS_CA",
        "AE_APISHIM_TLS_CA_CERT",
        "AE_APISHIM_TLS_CERT",
        "AE_APISHIM_ENV_FILE",
        "DEV_PROFILE_DIR",
        "AE_STATE_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_resolve_apishim_verify_prefers_tls_ca_cert(monkeypatch, tmp_path) -> None:
    ca_cert = tmp_path / "apishim.ca.crt"
    leaf_cert = tmp_path / "apishim.crt"
    ca_cert.write_text("demo-ca", encoding="utf-8")
    leaf_cert.write_text("demo-leaf", encoding="utf-8")

    monkeypatch.setenv("AE_APISHIM_TLS_CA_CERT", str(ca_cert))
    monkeypatch.setenv("AE_APISHIM_TLS_CERT", str(leaf_cert))

    assert http_api._resolve_apishim_verify() == str(ca_cert)


def test_resolve_apishim_verify_uses_profile_ca_neighbor(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "apishim.env"
    ca_cert = tmp_path / "apishim.ca.crt"
    leaf_cert = tmp_path / "apishim.crt"
    env_file.write_text("AE_APISHIM_SERVER=https://127.0.0.1:8445\n", encoding="utf-8")
    ca_cert.write_text("demo-ca", encoding="utf-8")
    leaf_cert.write_text("demo-leaf", encoding="utf-8")

    monkeypatch.setenv("AE_APISHIM_ENV_FILE", str(env_file))

    assert http_api._resolve_apishim_verify() == str(ca_cert)


def test_resolve_apishim_verify_does_not_use_leaf_cert_as_ca(monkeypatch, tmp_path) -> None:
    leaf_cert = tmp_path / "apishim.crt"
    leaf_cert.write_text("demo-leaf", encoding="utf-8")

    monkeypatch.setenv("AE_APISHIM_TLS_CERT", str(leaf_cert))

    assert http_api._resolve_apishim_verify() is False
