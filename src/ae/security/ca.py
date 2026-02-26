# ruff: noqa: S603,S607
"""
Lightweight CA helper for agent mTLS bootstrap using openssl.

We avoid pulling heavy crypto deps by shelling out to openssl. Artifacts live
under state/tls by default:
- CA key/cert: agent-ca.key / agent-ca.crt
- Issued per-node key/cert: <node_id>.key / <node_id>.crt
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ae._utc import UTC

DEFAULT_ROOT = Path("state/tls")
CA_KEY = "agent-ca.key"
CA_CRT = "agent-ca.crt"
ISSUED = "issued.json"
REVOKED = "revoked.json"
USED_TOKENS = "used_tokens.json"


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)


def ensure_ca(root: Path | str = DEFAULT_ROOT) -> tuple[Path, Path]:
    root = Path(root)
    _ensure_root(root)
    ca_key = root / CA_KEY
    ca_crt = root / CA_CRT
    if ca_key.exists() and ca_crt.exists():
        return ca_key, ca_crt
    subprocess.run(  # noqa: S603,S607 - openssl binary fixed; shell disabled
        [
            shutil.which("openssl") or "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_crt),
            "-subj",
            "/CN=ae-agent-ca",
            "-days",
            "3650",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ca_key, ca_crt


def issue_cert(
    node_id: str,
    *,
    root: Path | str = DEFAULT_ROOT,
    days: int = 365,
    ca_secret: str | None = None,
) -> tuple[Path, Path, Path]:
    """Return (cert, key, ca) paths for the issued node cert."""
    _ = ca_secret
    root = Path(root)
    ca_key, ca_crt = ensure_ca(root)
    key_path = root / f"{node_id}.key"
    csr_path = root / f"{node_id}.csr"
    crt_path = root / f"{node_id}.crt"
    serial_path = root / "agent-ca.srl"

    # Generate key + CSR
    subprocess.run(  # noqa: S603,S607 - openssl binary fixed; shell disabled
        [
            shutil.which("openssl") or "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(csr_path),
            "-subj",
            f"/CN={node_id}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Sign
    with tempfile.NamedTemporaryFile("w", delete=False) as ext:
        ext.write(
            "basicConstraints=CA:FALSE\n"
            "keyUsage = digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=clientAuth,serverAuth\n"
        )
        ext_path = ext.name
    try:
        subprocess.run(  # noqa: S603,S607 - openssl binary fixed; shell disabled
            [
                shutil.which("openssl") or "openssl",
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-CAserial",
                str(serial_path),
                "-CAcreateserial",
                "-out",
                str(crt_path),
                "-days",
                str(days),
                "-extfile",
                ext_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        with contextlib.suppress(Exception):
            Path(ext_path).unlink()
    with contextlib.suppress(Exception):
        csr_path.unlink()
    with contextlib.suppress(Exception):
        _record_issue(root, crt_path, node_id, days)
    return crt_path, key_path, ca_crt


def _record_issue(root: Path, crt_path: Path, node_id: str, days: int) -> None:
    """Persist issued cert metadata for revocation/rotation bookkeeping."""
    issued_path = root / ISSUED
    with contextlib.suppress(Exception):
        serial = (
            subprocess.check_output(  # noqa: S603,S607 - openssl binary fixed; shell disabled
                [
                    shutil.which("openssl") or "openssl",
                    "x509",
                    "-in",
                    str(crt_path),
                    "-noout",
                    "-serial",
                ],
                text=True,
            )  # noqa: S603,S607 - openssl binary fixed; shell disabled
            .strip()
            .split("=", 1)[-1]
        )
    serial = locals().get("serial", "")
    rec = {
        "node_id": node_id,
        "serial": serial,
        "cert": crt_path.name,
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
    }
    data = []
    if issued_path.exists():
        try:
            data = json.loads(issued_path.read_text())
        except Exception:
            data = []
    data.append(rec)
    issued_path.write_text(json.dumps(data, indent=2))


def record_used_token(token: str, root: Path | str = DEFAULT_ROOT) -> None:
    root = Path(root)
    used_path = root / USED_TOKENS
    data = []
    if used_path.exists():
        try:
            data = json.loads(used_path.read_text())
        except Exception:
            data = []
    data.append(token)
    used_path.write_text(json.dumps(data, indent=2))


def token_used(token: str, root: Path | str = DEFAULT_ROOT) -> bool:
    path = Path(root) / USED_TOKENS
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return token in data
    except Exception:
        return False


def revoke_serial(serial: str, root: Path | str = DEFAULT_ROOT) -> None:
    root = Path(root)
    path = root / REVOKED
    data = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = []
    if serial not in data:
        data.append(serial)
    path.write_text(json.dumps(data, indent=2))


def is_revoked(serial: str, root: Path | str = DEFAULT_ROOT) -> bool:
    path = Path(root) / REVOKED
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return serial in data
    except Exception:
        return False


def revoke_from_file(serial_file: str, root: Path | str = DEFAULT_ROOT) -> None:
    serial = Path(serial_file).read_text().strip()
    revoke_serial(serial, root=root)
