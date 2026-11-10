"""Activation-gated entry point for portable profile components.

The runner is intentionally not a general command launcher.  It accepts only
the installed Python component entry points required by the portable profile
contracts and waits for one byte from an inherited activation pipe
before importing either component.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

ComponentKind = Literal["controller", "node-local", "gateway", "node-agent"]
ACTIVATE_BYTE = b"1"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class ComponentGateError(RuntimeError):
    """The parent did not authorize this exact component to start."""


def await_activation(fd: int) -> None:
    """Block for one exact activation byte; EOF or extra bytes fail closed."""

    if type(fd) is not int or fd < 3:
        raise ComponentGateError("activation gate descriptor is invalid")
    try:
        payload = os.read(fd, 2)
    except OSError as exc:
        raise ComponentGateError("activation gate cannot be read") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if payload != ACTIVATE_BYTE:
        raise ComponentGateError("component activation was not authorized")


def run_component(
    kind: ComponentKind,
    argv: Sequence[str],
    *,
    gate_fd: int,
    engine_backend: str | None,
    engine_executable: Path | None,
    engine_device: int | None,
    engine_inode: int | None,
    host_plan_digest: str | None = None,
    host_receipt_digest: str | None = None,
    controller_main: Callable[[list[str] | None], int] | None = None,
    node_main: Callable[[list[str] | None], int] | None = None,
    gateway_main: Callable[[list[str] | None], int] | None = None,
) -> int:
    """Wait for activation and invoke one allowlisted in-process entry point."""

    if kind not in {"controller", "node-local", "gateway", "node-agent"}:
        raise ComponentGateError("component kind is not allowlisted")
    arguments = list(argv)
    if any(not isinstance(item, str) or "\x00" in item for item in arguments):
        raise ComponentGateError("component arguments are invalid")
    await_activation(gate_fd)
    if kind == "gateway":
        if any(
            value is not None
            for value in (
                engine_backend,
                engine_executable,
                engine_device,
                engine_inode,
                host_plan_digest,
                host_receipt_digest,
            )
        ):
            raise ComponentGateError("gateway component must not receive engine identity")
    elif kind == "node-agent" and engine_backend == "strict-cri":
        if any(value is not None for value in (engine_executable, engine_device, engine_inode)):
            raise ComponentGateError("strict-CRI node must not receive engine executable identity")
        if (
            type(host_plan_digest) is not str
            or type(host_receipt_digest) is not str
            or _DIGEST_RE.fullmatch(host_plan_digest) is None
            or _DIGEST_RE.fullmatch(host_receipt_digest) is None
        ):
            raise ComponentGateError("strict-CRI node authority is incomplete")
    else:
        if host_plan_digest is not None or host_receipt_digest is not None:
            raise ComponentGateError("engine component must not receive strict-CRI authority")
        if (
            engine_backend is None
            or engine_executable is None
            or engine_device is None
            or engine_inode is None
        ):
            raise ComponentGateError("engine-backed component identity is incomplete")
        validate_engine_identity(
            backend=engine_backend,
            executable=engine_executable,
            device=engine_device,
            inode=engine_inode,
        )
    if kind == "controller":
        if controller_main is not None:
            return int(controller_main(arguments))
        from ae.controller.__main__ import main as controller_entry

        return int(controller_entry(arguments))
    if kind == "gateway":
        if gateway_main is not None:
            return int(gateway_main(arguments))
        from ae.gateway.__main__ import main as gateway_entry

        return int(gateway_entry(arguments))
    if node_main is not None:
        return int(node_main(arguments))
    from ae.node.server import main as node_entry

    return int(node_entry(arguments))


def validate_engine_identity(*, backend: str, executable: Path, device: int, inode: int) -> None:
    """Fence launch on the exact executable identity captured before state."""

    if backend not in {"podman", "docker"}:
        raise ComponentGateError("component engine is not allowlisted")
    if not executable.is_absolute() or type(device) is not int or type(inode) is not int:
        raise ComponentGateError("component engine identity is invalid")
    if device < 0 or inode <= 0:
        raise ComponentGateError("component engine identity is invalid")
    try:
        info = executable.stat()
    except OSError as exc:
        raise ComponentGateError("component engine executable is unavailable") from exc
    if (info.st_dev, info.st_ino) != (device, inode):
        raise ComponentGateError("component engine executable identity changed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ae.kctl.profile_component_runner")
    parser.add_argument(
        "--gate-fd",
        type=int,
        default=int(os.environ.get("K1S_COMPONENT_GATE_FD", "-1")),
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=("controller", "node-local", "gateway", "node-agent"),
    )
    parser.add_argument(
        "--engine-backend", choices=("podman", "docker", "strict-cri"), default=None
    )
    parser.add_argument("--engine-executable", type=Path, default=None)
    parser.add_argument("--engine-device", type=int, default=None)
    parser.add_argument("--engine-inode", type=int, default=None)
    parser.add_argument("--host-plan-digest", default=None)
    parser.add_argument("--host-receipt-digest", default=None)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    arguments = list(namespace.arguments)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return run_component(
        cast(ComponentKind, namespace.kind),
        arguments,
        gate_fd=namespace.gate_fd,
        engine_backend=namespace.engine_backend,
        engine_executable=namespace.engine_executable,
        engine_device=namespace.engine_device,
        engine_inode=namespace.engine_inode,
        host_plan_digest=namespace.host_plan_digest,
        host_receipt_digest=namespace.host_receipt_digest,
    )


if __name__ == "__main__":  # pragma: no cover - console execution boundary
    raise SystemExit(main())


__all__ = [
    "ACTIVATE_BYTE",
    "ComponentGateError",
    "ComponentKind",
    "await_activation",
    "build_parser",
    "main",
    "run_component",
    "validate_engine_identity",
]
