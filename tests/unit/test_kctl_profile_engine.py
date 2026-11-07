from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ae.kctl.profile_engine import (
    CommandResult,
    EngineCapabilityError,
    SelectedEngineProbe,
)


class Resolver:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    def resolve(self, name: str) -> Path:
        self.calls.append(name)
        return self.path


class Runner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float, int]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult:
        self.calls.append((tuple(argv), dict(environment), timeout_seconds, output_limit))
        return self.responses.pop(0)


def _executable(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("synthetic executable", encoding="utf-8")
    path.chmod(0o755)
    return path


def _result(value: object, *, returncode: int = 0) -> CommandResult:
    return CommandResult(returncode, json.dumps(value).encode(), b"")


def test_rootless_podman_capability_is_exact_and_source_unqualified(tmp_path: Path) -> None:
    executable = _executable(tmp_path, "podman")
    runner = Runner(
        [
            _result({"Client": {"Version": "5.4.2"}}),
        ]
    )
    resolver = Resolver(executable)

    capability = SelectedEngineProbe(runner=runner, resolver=resolver).probe("podman")

    assert resolver.calls == ["podman"]
    assert capability.backend == "podman"
    assert capability.executable == executable
    assert capability.version == "5.4.2"
    assert capability.rootless is True
    assert capability.qualification_posture == "source-validated/unqualified"
    assert capability.digest().startswith("sha256:")
    assert len(runner.calls) == 1
    assert all(call[0][0] == str(executable) for call in runner.calls)
    assert all(
        call[1] == {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "TZ": "UTC"} for call in runner.calls
    )


def test_rootful_podman_fails_without_docker_fallback(tmp_path: Path) -> None:
    executable = _executable(tmp_path, "podman")
    runner = Runner(
        [
            _result({"Client": {"Version": "5.4.2"}}),
        ]
    )
    resolver = Resolver(executable)

    with pytest.raises(EngineCapabilityError, match="not rootless"):
        SelectedEngineProbe(runner=runner, resolver=resolver, effective_uid=0).probe("podman")

    assert resolver.calls == ["podman"]
    assert all("docker" not in call[0] for call in runner.calls)


def test_docker_is_probed_only_when_explicitly_selected(tmp_path: Path) -> None:
    executable = _executable(tmp_path, "docker")
    runner = Runner(
        [
            _result({"Server": {"Version": "27.4.0", "Os": "linux"}}),
        ]
    )

    capability = SelectedEngineProbe(runner=runner, resolver=Resolver(executable)).probe("docker")

    assert capability.backend == "docker"
    assert capability.version == "27.4.0"
    assert capability.rootless is False
    assert [call[0][1] for call in runner.calls] == ["version"]


@pytest.mark.parametrize(
    ("backend", "responses", "match"),
    [
        ("other", [], "explicitly podman or docker"),
        ("podman", [CommandResult(1, b"{}", b"denied")], "returned nonzero"),
        ("podman", [CommandResult(0, b"not-json", b"")], "not JSON"),
    ],
)
def test_invalid_engine_capability_fails_closed(
    tmp_path: Path,
    backend: str,
    responses: list[CommandResult],
    match: str,
) -> None:
    executable = _executable(tmp_path, "engine")
    with pytest.raises(EngineCapabilityError, match=match):
        SelectedEngineProbe(runner=Runner(responses), resolver=Resolver(executable)).probe(backend)
