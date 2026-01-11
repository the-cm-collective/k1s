"""
Lightweight CA helper for agent mTLS bootstrap using openssl.

We avoid pulling heavy crypto deps by shelling out to openssl. Artifacts live
under state/tls by default:
- CA key/cert: agent-ca.key / agent-ca.crt
- Issued per-node key/cert: <node_id>.key / <node_id>.crt
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import json
from datetime import datetime, timedelta, timezone

DEFAULT_ROOT = Path("state/tls")
CA_KEY = "agent-ca.key"
CA_CRT = "agent-ca.crt"
ISSUED = "issued.json"


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)


def ensure_ca(root: Path | str = DEFAULT_ROOT) -> tuple[Path, Path]:
    root = Path(root)
    _ensure_root(root)
    ca_key = root / CA_KEY
    ca_crt = root / CA_CRT
    if ca_key.exists() and ca_crt.exists():
        return ca_key, ca_crt
    subprocess.run(
        [
            "openssl",
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
    root = Path(root)
    ca_key, ca_crt = ensure_ca(root)
    key_path = root / f"{node_id}.key"
    csr_path = root / f"{node_id}.csr"
    crt_path = root / f"{node_id}.crt"

    # Generate key + CSR
    subprocess.run(
        [
            "openssl",
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
        ext.write("basicConstraints=CA:FALSE\nkeyUsage = digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth,serverAuth\n")
        ext_path = ext.name
    try:
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
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
        try:
            Path(ext_path).unlink()
        except Exception:
            pass
    try:
        csr_path.unlink()
    except Exception:
        pass
    try:
        _record_issue(root, crt_path, node_id, days)
    except Exception:
        pass
    return crt_path, key_path, ca_crt


def _record_issue(root: Path, crt_path: Path, node_id: str, days: int) -> None:
    """Persist issued cert metadata for revocation/rotation bookkeeping."""
    issued_path = root / ISSUED
    try:
        serial = (
            subprocess.check_output(
                ["openssl", "x509", "-in", str(crt_path), "-noout", "-serial"],
                text=True,
            )
            .strip()
            .split("=", 1)[-1]
        )
    except Exception:
        serial = ""
    rec = {
        "node_id": node_id,
        "serial": serial,
        "cert": crt_path.name,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
    }
    data = []
    if issued_path.exists():
        try:
            data = json.loads(issued_path.read_text())
        except Exception:
            data = []
    data.append(rec)
    issued_path.write_text(json.dumps(data, indent=2))
