"""Resource loaders for packaged text assets."""

from __future__ import annotations

from functools import cache
from importlib import resources


@cache
def load_text(*parts: str) -> str:
    if not parts:
        raise ValueError("load_text requires at least one path component")
    path = resources.files("ae.resources")
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8")


def render_text(*parts: str, **replacements: str) -> str:
    text = load_text(*parts)
    for key, value in replacements.items():
        text = text.replace(f"__{key}__", str(value))
    return text
