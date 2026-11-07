from __future__ import annotations

import os
from pathlib import Path

import pytest

from ae.kctl.profile_component_runner import (
    ComponentGateError,
    run_component,
    validate_engine_identity,
)


def _gate(payload: bytes) -> int:
    read_fd, write_fd = os.pipe()
    if payload:
        os.write(write_fd, payload)
    os.close(write_fd)
    return read_fd


def _engine(tmp_path: Path) -> tuple[Path, int, int]:
    executable = tmp_path / "podman"
    executable.write_text("synthetic", encoding="utf-8")
    executable.chmod(0o755)
    info = executable.stat()
    return executable, info.st_dev, info.st_ino


def test_activation_gate_invokes_only_allowlisted_controller_after_exact_byte(
    tmp_path: Path,
) -> None:
    calls: list[list[str] | None] = []
    executable, device, inode = _engine(tmp_path)

    result = run_component(
        "controller",
        ["--loop", "--metrics-port", "9108"],
        gate_fd=_gate(b"1"),
        engine_backend="podman",
        engine_executable=executable,
        engine_device=device,
        engine_inode=inode,
        controller_main=lambda argv: calls.append(argv) or 7,
    )

    assert result == 7
    assert calls == [["--loop", "--metrics-port", "9108"]]


def test_activation_gate_invokes_node_without_shell_or_exec(tmp_path: Path) -> None:
    calls: list[list[str] | None] = []
    executable, device, inode = _engine(tmp_path)

    result = run_component(
        "node-local",
        ["--host", "127.0.0.1"],
        gate_fd=_gate(b"1"),
        engine_backend="podman",
        engine_executable=executable,
        engine_device=device,
        engine_inode=inode,
        node_main=lambda argv: calls.append(argv) or 0,
    )

    assert result == 0
    assert calls == [["--host", "127.0.0.1"]]


@pytest.mark.parametrize("payload", [b"", b"0", b"11"])
def test_gate_eof_or_noncanonical_activation_fails_closed(payload: bytes, tmp_path: Path) -> None:
    executable, device, inode = _engine(tmp_path)
    with pytest.raises(ComponentGateError, match="not authorized"):
        run_component(
            "controller",
            [],
            gate_fd=_gate(payload),
            engine_backend="podman",
            engine_executable=executable,
            engine_device=device,
            engine_inode=inode,
            controller_main=lambda _argv: pytest.fail("component must remain blocked"),
        )


def test_unknown_component_or_nul_argument_fails_before_gate_read() -> None:
    with pytest.raises(ComponentGateError, match="not allowlisted"):
        run_component(
            "other",
            [],
            gate_fd=-1,
            engine_backend="podman",
            engine_executable=Path("/usr/bin/podman"),
            engine_device=1,
            engine_inode=1,
        )  # type: ignore[arg-type]
    with pytest.raises(ComponentGateError, match="arguments"):
        run_component(
            "controller",
            ["bad\x00arg"],
            gate_fd=-1,
            engine_backend="podman",
            engine_executable=Path("/usr/bin/podman"),
            engine_device=1,
            engine_inode=1,
        )


def test_engine_executable_identity_mismatch_fails_before_component_import(
    tmp_path: Path,
) -> None:
    executable, device, inode = _engine(tmp_path)

    with pytest.raises(ComponentGateError, match="identity changed"):
        validate_engine_identity(
            backend="podman",
            executable=executable,
            device=device,
            inode=inode + 1,
        )
