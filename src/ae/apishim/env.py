"""Packaged API shim environment and TLS helper."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path


def ensure_local_apishim_env(
    *,
    env_file: str | Path | None = None,
    env_override_file: str | Path | None = None,
    cert_file: str | Path | None = None,
    key_file: str | Path | None = None,
    ca_file: str | Path | None = None,
    ca_key_file: str | Path | None = None,
    cert_sans: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Write API shim token env and best-effort local TLS material.

    This mirrors the dev shell helper in a package-safe form so downstream
    tools do not need access to the k1s repository's ``scripts/`` directory.
    """

    env = dict(os.environ if environ is None else environ)
    env_path = Path(env_file or env.get("APISHIM_ENV_FILE") or "state/profiles/labs/apishim.env")
    override_path = Path(env_override_file or env.get("APISHIM_ENV_OVERRIDE_FILE") or ".env")

    values = {
        "AE_APISHIM_TOKEN": _resolve_secret("AE_APISHIM_TOKEN", env, override_path, env_path),
        "AE_APISHIM_READ_TOKEN": _resolve_secret(
            "AE_APISHIM_READ_TOKEN", env, override_path, env_path
        ),
        "AE_APISHIM_SESSION_SECRET": _resolve_secret(
            "AE_APISHIM_SESSION_SECRET", env, override_path, env_path, min_len=32
        ),
        "AE_APISHIM_MINT_TOKEN": _resolve_secret(
            "AE_APISHIM_MINT_TOKEN", env, override_path, env_path
        ),
        "AE_API_ADMIN_TOKEN": _resolve_secret("AE_API_ADMIN_TOKEN", env, override_path, env_path),
    }
    labs_token = _read_value("AE_LABS_TOKEN", env, override_path, env_path)
    values["AE_LABS_TOKEN"] = labs_token or values["AE_APISHIM_TOKEN"]
    values["AE_LABS_HELM_TOKEN"] = values["AE_APISHIM_TOKEN"]

    env_path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        env_path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
    finally:
        os.umask(old_umask)
    env_path.chmod(0o600)

    cert_path = Path(cert_file or env.get("APISHIM_CERT_FILE") or "state/profiles/labs/apishim.crt")
    key_path = Path(key_file or env.get("APISHIM_KEY_FILE") or "state/profiles/labs/apishim.key")
    ca_path = Path(ca_file or env.get("APISHIM_CA_FILE") or cert_path.parent / "apishim.ca.crt")
    ca_key_path = Path(
        ca_key_file or env.get("APISHIM_CA_KEY_FILE") or key_path.parent / "apishim.ca.key"
    )
    san = (
        cert_sans
        or env.get("APISHIM_CERT_SANS")
        or "DNS:apishim,DNS:localhost,IP:127.0.0.1,IP:::1"
    )
    if _tls_material_missing(cert_path, key_path, ca_path, ca_key_path):
        _generate_tls(cert_path, key_path, ca_path, ca_key_path, san)

    values.update(
        {
            "APISHIM_ENV_FILE": str(env_path),
            "APISHIM_CERT_FILE": str(cert_path),
            "APISHIM_KEY_FILE": str(key_path),
            "APISHIM_CA_FILE": str(ca_path),
            "APISHIM_CA_KEY_FILE": str(ca_key_path),
        }
    )
    return values


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _read_value(key: str, env: Mapping[str, str], override_path: Path, env_path: Path) -> str:
    if env.get(key):
        return str(env[key])
    override = _read_env_file(override_path)
    if override.get(key):
        return override[key]
    existing = _read_env_file(env_path)
    return existing.get(key, "")


def _resolve_secret(
    key: str,
    env: Mapping[str, str],
    override_path: Path,
    env_path: Path,
    *,
    min_len: int = 24,
) -> str:
    value = _read_value(key, env, override_path, env_path)
    if value and len(value) >= min_len:
        return value
    return secrets.token_urlsafe(32)


def _tls_material_missing(*paths: Path) -> bool:
    return any(not path.is_file() or path.stat().st_size == 0 for path in paths)


def _generate_tls(
    cert_path: Path,
    key_path: Path,
    ca_path: Path,
    ca_key_path: Path,
    san: str,
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        return
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    ca_path.parent.mkdir(parents=True, exist_ok=True)
    ca_key_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ae-apishim-tls-") as tmp:
        tmp_path = Path(tmp)
        csr = tmp_path / "apishim.csr"
        ext = tmp_path / "apishim.ext"
        subprocess.run(  # noqa: S603,S607 - fixed local openssl invocation for dev TLS.
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-days",
                "3",
                "-nodes",
                "-keyout",
                str(ca_key_path),
                "-out",
                str(ca_path),
                "-subj",
                "/CN=apishim-dev-ca",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        ext.write_text(
            "\n".join(
                [
                    "basicConstraints=critical,CA:FALSE",
                    "keyUsage=critical,digitalSignature,keyEncipherment",
                    "extendedKeyUsage=serverAuth",
                    f"subjectAltName={san}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603,S607 - fixed local openssl invocation for dev TLS.
            [
                openssl,
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-keyout",
                str(key_path),
                "-out",
                str(csr),
                "-subj",
                "/CN=apishim",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(  # noqa: S603,S607 - fixed local openssl invocation for dev TLS.
            [
                openssl,
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_path),
                "-CAkey",
                str(ca_key_path),
                "-CAcreateserial",
                "-out",
                str(cert_path),
                "-days",
                "3",
                "-sha256",
                "-extfile",
                str(ext),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    serial = ca_path.with_suffix(ca_path.suffix + ".srl")
    if serial.exists():
        serial.unlink()
    for path in (cert_path, key_path, ca_path, ca_key_path):
        if path.exists():
            path.chmod(0o600)
