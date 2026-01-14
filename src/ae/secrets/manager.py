"""Secret management helpers powered by SOPS/age."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

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
            allow_plaintext if allow_plaintext is not None else allow_plaintext_env == "1"
        )

    def load_env(self, refs: Iterable[SecretRef]) -> dict[str, str]:
        env: dict[str, str] = {}
        for ref in refs:
            decrypted = self._decrypt(Path(ref.path))
            for mapping in ref.env:
                if mapping.key not in decrypted:
                    raise KeyError(
                        f"Secret {ref.name} missing key '{mapping.key}' "
                        f"referenced by {mapping.name}"
                    )
                env[mapping.name] = decrypted[mapping.key]
        return env

    def _decrypt(self, path: Path) -> dict[str, str]:
        if not path.exists():
            raise FileNotFoundError(f"Secret file {path} not found")

        # Fast-path: if plaintext allowed and file does not look like a SOPS file,
        # parse and return it directly to avoid invoking sops and emitting errors.
        if self._allow_plaintext:
            with suppress(Exception):
                raw = path.read_text()
                with suppress(json.JSONDecodeError):
                    probe = json.loads(raw)
                if "probe" not in locals():
                    probe = yaml.safe_load(raw)
                if isinstance(probe, dict) and "sops" not in probe:
                    return {str(k): str(v) for k, v in probe.items()}

        # Try sops a few times to ride out transient startup races; if plaintext is allowed,
        # fall back to direct read on failure to avoid noisy crashes during demos.
        attempts = 3
        delay = 0.3
        for i in range(attempts):
            try:
                # Safe: sops binary path is operator-controlled, not user input.
                completed = subprocess.run(  # noqa: S603
                    [self._sops, "--decrypt", str(path)],  # noqa: S603
                    check=True,
                    capture_output=True,
                    text=True,
                )
                content = completed.stdout
                break
            except FileNotFoundError as exc:
                if not self._allow_plaintext:
                    raise RuntimeError(
                        "SOPS binary not found and plaintext secrets are disabled"
                    ) from exc
                content = path.read_text()
                break
            except subprocess.CalledProcessError as exc:
                # If plaintext permitted, read raw file immediately to reduce log noise.
                if self._allow_plaintext:
                    content = path.read_text()
                    break
                # Otherwise, brief retry to handle transient sops/env setup races.
                if i < attempts - 1:
                    import time as _t

                    _t.sleep(delay)
                    continue
                raise RuntimeError(f"sops decrypt failed for {path}: {exc.stderr}") from exc

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = yaml.safe_load(content)

        if not isinstance(data, dict):
            raise ValueError(f"Decrypted secret {path} must produce a mapping")
        return {str(key): str(value) for key, value in data.items()}
