"""Fail-closed loaders for deterministic package resources."""

from __future__ import annotations

from functools import cache
from importlib import resources


def _validate_parts(parts: tuple[str, ...]) -> None:
    if not parts:
        raise ValueError("resource path requires at least one component")
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            raise ValueError(f"unsafe resource path component: {part!r}")


@cache
def load_binary(*parts: str) -> bytes:
    """Read one package resource addressed by canonical path components."""

    _validate_parts(parts)
    path = resources.files("ae.resources")
    for part in parts:
        path = path / part
    return path.read_bytes()


@cache
def load_text(*parts: str) -> str:
    """Read one UTF-8 package resource without a filesystem fallback."""

    return load_binary(*parts).decode("utf-8")


def render_text(*parts: str, **replacements: str) -> str:
    text = load_text(*parts)
    for key, value in replacements.items():
        text = text.replace(f"__{key}__", str(value))
    return text
