"""TLS secret sync helper.

Resolves K8s-style TLS Secret material (tls.crt/tls.key) into local cert/key
files that the Caddy ingress can reference. Supports two lookup modes:

1) Direct PEM files at <root>/<name>.crt and <root>/<name>.key
2) K8s Secret YAML/JSON at <root>/<name>.yaml|yml|json with base64 data

When given a Secret file, it writes decoded PEMs under
<root>/rendered/<name>.crt and <root>/rendered/<name>.key.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

import yaml


class TlsSecretResolver:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def resolve(self, name: str) -> Optional[tuple[Path, Path]]:
        if not name:
            return None
        # 1) Direct files
        crt = self._root / f"{name}.crt"
        key = self._root / f"{name}.key"
        if crt.exists() and key.exists():
            return crt, key

        # 2) Secret YAML/JSON
        for ext in ("yaml", "yml", "json"):
            cand = self._root / f"{name}.{ext}"
            if cand.exists():
                data = self._load_secret(cand)
                if not data:
                    continue
                tls_crt_b64 = data.get("data", {}).get("tls.crt")
                tls_key_b64 = data.get("data", {}).get("tls.key")
                if not tls_crt_b64 or not tls_key_b64:
                    continue
                try:
                    crt_bytes = base64.b64decode(tls_crt_b64)
                    key_bytes = base64.b64decode(tls_key_b64)
                except Exception:
                    continue
                out_dir = self._root / "rendered"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_crt = out_dir / f"{name}.crt"
                out_key = out_dir / f"{name}.key"
                out_crt.write_bytes(crt_bytes)
                out_key.write_bytes(key_bytes)
                try:
                    out_crt.chmod(0o600)
                    out_key.chmod(0o600)
                except Exception:
                    pass
                return out_crt, out_key
        return None

    def _load_secret(self, path: Path) -> Optional[dict]:
        try:
            if path.suffix.lower() == ".json":
                return json.loads(path.read_text())
            return yaml.safe_load(path.read_text())
        except Exception:
            return None


# ruff: noqa: E501,UP006,UP007,S110,S112
