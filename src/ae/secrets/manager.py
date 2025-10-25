"""Secret management helpers powered by SOPS/age."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable

import yaml

from ae.controller.spec import SecretRef


class SecretManager:
    """Decrypts sealed secrets and projects them into environment variables."""

    def __init__(
        self,
        *,
        sops_binary: str | None = None,
        allow_plaintext: bool | None = None,
    ) -> None:
        self._sops = sops_binary or os.getenv("AE_SOPS_BIN", "sops")
        allow_plaintext_env = os.getenv("AE_ALLOW_PLAINTEXT_SECRETS")
        self._allow_plaintext = (
            allow_plaintext
            if allow_plaintext is not None
            else allow_plaintext_env == "1"
        )

    def load_env(self, refs: Iterable[SecretRef]) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for ref in refs:
            decrypted = self._decrypt(Path(ref.path))
            for mapping in ref.env:
                if mapping.key not in decrypted:
                    raise KeyError(
                        f"Secret {ref.name} missing key '{mapping.key}' referenced by {mapping.name}"
                    )
                env[mapping.name] = decrypted[mapping.key]
        return env

    def _decrypt(self, path: Path) -> Dict[str, str]:
        if not path.exists():
            raise FileNotFoundError(f"Secret file {path} not found")

        # Try sops a few times to ride out transient startup races; if plaintext is allowed,
        # fall back to direct read on failure to avoid noisy crashes during demos.
        attempts = 3
        delay = 0.3
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                completed = subprocess.run(  # noqa: S603
                    [self._sops, "--decrypt", str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                content = completed.stdout
                break
            except FileNotFoundError as exc:
                last_err = exc
                if not self._allow_plaintext:
                    raise RuntimeError(
                        "SOPS binary not found and plaintext secrets are disabled"
                    ) from exc
                content = path.read_text()
                break
            except subprocess.CalledProcessError as exc:
                last_err = exc
                # If plaintext permitted, read raw file immediately to reduce log noise.
                if self._allow_plaintext:
                    content = path.read_text()
                    break
                # Otherwise, brief retry to handle transient sops/env setup races.
                if i < attempts - 1:
                    try:
                        import time as _t
                        _t.sleep(delay)
                    except Exception:
                        pass
                    continue
                raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = yaml.safe_load(content)

        if not isinstance(data, dict):
            raise ValueError(f"Decrypted secret {path} must produce a mapping")
        return {str(key): str(value) for key, value in data.items()}
