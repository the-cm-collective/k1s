"""Config management helpers (YAML/JSON to environment variables)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import yaml

from ae.controller.spec import ConfigRef


class ConfigManager:
    """Loads config files and projects selected keys into environment variables."""

    def load_env(self, refs: Iterable[ConfigRef]) -> dict[str, str]:
        env: dict[str, str] = {}
        for ref in refs:
            data = self._load(Path(ref.path))
            for mapping in ref.env:
                if mapping.key not in data:
                    raise KeyError(
                        f"Config {ref.name} missing key '{mapping.key}' "
                        f"referenced by {mapping.name}"
                    )
                env[mapping.name] = str(data[mapping.key])
        return env

    def _load(self, path: Path) -> dict[str, object]:
        if not path.exists():
            raise FileNotFoundError(f"Config file {path} not found")
        content = path.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError(f"Config {path} must produce a mapping")
        return data
