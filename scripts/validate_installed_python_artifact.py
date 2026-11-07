#!/usr/bin/env python3
"""Install and smoke-test a k1s-core wheel with no package-index or network access."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

LOCK_RE = re.compile(
    r"^(?P<requirement>[^#\s][^#]*?)\s+--hash=sha256:(?P<digest>[0-9a-f]{64})"
    r"\s+#\s+(?P<filename>\S+\.whl)\s*$"
)
INDEX_ENV_KEYS = {
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _locked_wheels(lock: Path) -> dict[str, str]:
    wheels: dict[str, str] = {}
    for line_number, line in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = LOCK_RE.fullmatch(stripped)
        if match is None:
            raise ValueError(f"malformed lock entry at {lock}:{line_number}")
        filename = match.group("filename")
        if filename in wheels:
            raise ValueError(f"duplicate locked wheel: {filename}")
        wheels[filename] = match.group("digest")
    if not wheels:
        raise ValueError(f"empty wheel lock: {lock}")
    return wheels


def verify_wheelhouse(wheelhouse: Path, lock: Path) -> None:
    """Require the local wheelhouse to match the reviewed lock exactly."""
    expected = _locked_wheels(lock)
    actual = {path.name: _sha256(path) for path in wheelhouse.glob("*.whl") if path.is_file()}
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
        )
        raise ValueError(
            f"wheelhouse does not match lock: missing={missing}, extra={extra}, changed={changed}"
        )


def offline_environment(*, home: Path, temporary: Path, guard: Path) -> dict[str, str]:
    """Return a credential-minimal environment with indexes and sockets disabled."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in INDEX_ENV_KEYS and "proxy" not in key.lower()
    }
    env.update(
        {
            "AE_RUNTIME_BACKEND": "stub",
            "HOME": str(home),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(guard),
            "TMPDIR": str(temporary),
        }
    )
    return env


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    print("+", " ".join(argv))
    subprocess.run(argv, cwd=cwd, env=env, check=True)  # noqa: S603


def validate_install(
    *, wheel: Path, wheelhouse: Path, lock: Path, work_root: Path, expected_version: str
) -> None:
    if work_root.exists():
        raise ValueError(f"work root already exists: {work_root}")
    work_root.mkdir(mode=0o700, parents=True)
    if work_root.stat().st_mode & 0o077:
        raise ValueError(f"work root is not owner-private: {work_root}")
    verify_wheelhouse(wheelhouse, lock)

    home = work_root / "home"
    temporary = work_root / "tmp"
    outside = work_root / "outside-checkout"
    guard = work_root / "network-guard"
    for path in (home, temporary, outside, guard):
        path.mkdir(mode=0o700)
    (guard / "sitecustomize.py").write_text(
        """import socket

def _deny(*_args, **_kwargs):
    raise RuntimeError("network access disabled by installed-artifact gate")

socket.create_connection = _deny
socket.getaddrinfo = _deny
socket.socket.connect = _deny
socket.socket.connect_ex = _deny
""",
        encoding="utf-8",
    )
    env = offline_environment(home=home, temporary=temporary, guard=guard)

    venv = work_root / "venv"
    _run([sys.executable, "-m", "venv", str(venv)], cwd=outside, env=env)
    python = venv / "bin" / "python"
    bin_dir = venv / "bin"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            str(wheel),
        ],
        cwd=outside,
        env=env,
    )
    _run([str(python), "-m", "pip", "check"], cwd=outside, env=env)
    _run(
        [
            str(python),
            "-c",
            """from importlib import metadata, resources
import ae
from ae.apishim.server import _openapi_v3_stub, _swagger_doc
from ae.runtime.cri.api.runtime.v1 import api_pb2, gogo_pb2
expected = __import__('os').environ['K1S_EXPECTED_VERSION']
assert ae.__version__ == expected
assert metadata.version('k1s-core') == expected
assert _swagger_doc()['info']['version'] == expected
assert _openapi_v3_stub()['info']['version'] == expected
assert api_pb2.DESCRIPTOR.dependencies[0] is gogo_pb2.DESCRIPTOR
assert resources.files('ae.resources').joinpath('docs/loader.md').is_file()
scripts = {
    entry.name
    for entry in metadata.distribution('k1s-core').entry_points
    if entry.group == 'console_scripts'
}
assert scripts == {'ae', 'ae-rotate-certs', 'k1s', 'ae-node', 'ae-gateway', 'ae-worker-stub'}
""",
        ],
        cwd=outside,
        env={**env, "K1S_EXPECTED_VERSION": expected_version},
    )
    for command in (
        ("ae", "--help"),
        ("ae", "version"),
        ("k1s", "--help"),
        ("ae-rotate-certs", "--help"),
        ("ae-node", "--help"),
        ("ae-gateway", "--help"),
        ("ae-worker-stub", "--help"),
    ):
        _run([str(bin_dir / command[0]), *command[1:]], cwd=outside, env=env)

    manifest = outside / "artifact-smoke.yaml"
    manifest.write_text(
        """apiVersion: ae.dev/v1alpha1
kind: Deployment
metadata:
  name: artifact-smoke
spec:
  replicas: 1
  image: example.invalid/artifact-smoke:1
""",
        encoding="utf-8",
    )
    _run([str(bin_dir / "ae"), "plan", "-f", str(manifest), "--json"], cwd=outside, env=env)

    shutil.rmtree(guard)
    if str(Path.cwd().resolve()) in str(outside.resolve()):
        raise ValueError("installed-artifact smoke unexpectedly ran inside the checkout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--requirements-lock", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    validate_install(
        wheel=args.wheel.resolve(),
        wheelhouse=args.wheelhouse.resolve(),
        lock=args.requirements_lock.resolve(),
        work_root=args.work_root.resolve(),
        expected_version=args.expected_version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
