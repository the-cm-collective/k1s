"""Kubernetes command/args translation helpers for runtime adapters."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def kubernetes_command_parts(
    command: Iterable[Any] | None,
    args: Iterable[Any] | None,
) -> tuple[list[str] | None, list[str] | None]:
    """Return OCI entrypoint and command arguments for Kubernetes command/args.

    Kubernetes `command` maps to the container entrypoint. Kubernetes `args`
    maps to the arguments passed to that entrypoint. CLI runtimes usually expose
    entrypoint as a separate flag, so command elements after index 0 become
    runtime arguments.
    """

    command_items = _items(command)
    arg_items = _items(args)
    if command_items:
        return command_items[:1], command_items[1:] + arg_items or None
    return None, arg_items or None


def _items(value: Iterable[Any] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value if item is not None]
    except TypeError:
        return [str(value)]
