#!/usr/bin/env python3
"""Create the reviewed release wheelhouse, including pinned source-only wheels."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

try:
    from scripts.normalize_python_wheel import normalize as normalize_wheel
    from scripts.validate_installed_python_artifact import verify_wheelhouse
except ModuleNotFoundError:  # direct execution from scripts/
    from normalize_python_wheel import normalize as normalize_wheel  # type: ignore[no-redef]
    from validate_installed_python_artifact import (  # type: ignore[no-redef]
        verify_wheelhouse,
    )

SCHEMA = "k1s-release-source-wheels/v1"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LOCK_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s]+) "
    r"--hash=sha256:(?P<sha256>[0-9a-f]{64})  # (?P<filename>[^\s]+\.whl)$"
)


class WheelhousePolicyError(ValueError):
    """The source-wheel closure differs from the reviewed policy."""


class WheelhouseCommandError(RuntimeError):
    """A bounded wheelhouse command failed."""


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    sha256: str
    filename: str
    line: str


@dataclass(frozen=True)
class SourceWheel:
    name: str
    version: str
    source_url: str
    source_filename: str
    source_root: str
    source_sha256: str
    wheel_filename: str
    wheel_sha256: str
    python_version: str
    build_requirements: dict[str, str]


@dataclass(frozen=True)
class SourceWheelContract:
    source_date_epoch: int
    packages: tuple[SourceWheel, ...]


def _fail(message: str) -> NoReturn:
    raise WheelhousePolicyError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_keys(value: object, expected: set[str], *, label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        _fail(f"{label} shape differs")
    return value


def _text(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or "\x00" in value or "\n" in value:
        _fail(f"{label} differs")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{label} differs")
    return value


def load_contract(path: Path) -> SourceWheelContract:
    try:
        raw = path.read_bytes()
        if len(raw) > 128 * 1024:
            _fail("source-wheel contract exceeds its size ceiling")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise WheelhousePolicyError("source-wheel contract is unavailable or malformed") from exc
    root = _exact_keys(value, {"packages", "schema", "sourceDateEpoch"}, label="contract")
    if root["schema"] != SCHEMA or type(root["sourceDateEpoch"]) is not int:
        _fail("source-wheel contract identity differs")
    epoch = root["sourceDateEpoch"]
    if epoch < 315532800 or epoch > 4354819198 or epoch % 2:
        _fail("source-wheel epoch is not a canonical ZIP timestamp")
    packages_value = root["packages"]
    if type(packages_value) is not list or not packages_value:
        _fail("source-wheel package inventory differs")
    packages: list[SourceWheel] = []
    identities: set[str] = set()
    filenames: set[str] = set()
    for index, item in enumerate(packages_value):
        package = _exact_keys(
            item,
            {"build", "name", "source", "version", "wheel"},
            label=f"package {index}",
        )
        build = _exact_keys(
            package["build"], {"python", "requirements"}, label=f"package {index} build"
        )
        requirements = _exact_keys(
            build["requirements"],
            {"build", "packaging", "pyproject-hooks", "setuptools", "wheel"},
            label=f"package {index} build requirements",
        )
        source = _exact_keys(
            package["source"],
            {"filename", "root", "sha256", "url"},
            label=f"package {index} source",
        )
        wheel = _exact_keys(
            package["wheel"], {"filename", "sha256"}, label=f"package {index} wheel"
        )
        name = _text(package["name"], label="package name", pattern=re.compile(r"[A-Za-z0-9_.-]+"))
        version = _text(package["version"], label="package version")
        canonical = name.lower().replace("_", "-")
        if canonical in identities:
            _fail("source-wheel package identity is duplicated")
        identities.add(canonical)
        source_filename = _text(source["filename"], label="source filename")
        wheel_filename = _text(wheel["filename"], label="wheel filename")
        if (
            PurePosixPath(source_filename).name != source_filename
            or PurePosixPath(wheel_filename).name != wheel_filename
            or source_filename in filenames
            or wheel_filename in filenames
        ):
            _fail("source-wheel filename differs or collides")
        filenames.update((source_filename, wheel_filename))
        packages.append(
            SourceWheel(
                name=name,
                version=version,
                source_url=_text(source["url"], label="source URL"),
                source_filename=source_filename,
                source_root=_text(source["root"], label="source root"),
                source_sha256=_text(source["sha256"], label="source digest", pattern=SHA256_RE),
                wheel_filename=wheel_filename,
                wheel_sha256=_text(wheel["sha256"], label="wheel digest", pattern=SHA256_RE),
                python_version=_text(build["python"], label="build Python"),
                build_requirements={
                    key: _text(requirements[key], label=f"build requirement {key}")
                    for key in sorted(requirements)
                },
            )
        )
    return SourceWheelContract(source_date_epoch=epoch, packages=tuple(packages))


def load_lock(path: Path) -> tuple[LockedWheel, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WheelhousePolicyError("release requirements lock is unavailable") from exc
    locked: list[LockedWheel] = []
    identities: set[str] = set()
    filenames: set[str] = set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = LOCK_RE.fullmatch(line)
        if match is None:
            _fail("release requirements lock contains an unsupported line")
        value = LockedWheel(line=line, **match.groupdict())
        identity = value.name.lower().replace("_", "-")
        if identity in identities or value.filename in filenames:
            _fail("release requirements lock contains a duplicate")
        identities.add(identity)
        filenames.add(value.filename)
        locked.append(value)
    if not locked:
        _fail("release requirements lock is empty")
    return tuple(locked)


def validate_contract_against_lock(
    contract: SourceWheelContract, locked: tuple[LockedWheel, ...]
) -> tuple[LockedWheel, ...]:
    by_name = {item.name.lower().replace("_", "-"): item for item in locked}
    source_names: set[str] = set()
    for package in contract.packages:
        name = package.name.lower().replace("_", "-")
        expected = by_name.get(name)
        if expected is None:
            _fail("source-wheel package is absent from the release lock")
        if (
            expected.version != package.version
            or expected.filename != package.wheel_filename
            or expected.sha256 != package.wheel_sha256
        ):
            _fail("source-wheel package differs from the release lock")
        source_names.add(name)
    return tuple(item for item in locked if item.name.lower().replace("_", "-") not in source_names)


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    if not url.startswith("https://files.pythonhosted.org/"):
        _fail("source-wheel URL is outside the reviewed Python package host")
    request = urllib.request.Request(  # noqa: S310 - exact HTTPS host and content hash
        url, headers={"User-Agent": "k1s-release-wheelhouse/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - pinned host/hash
            if response.status != 200 or response.geturl() != url:
                _fail("source-wheel download redirected or returned a non-success status")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_ARCHIVE_BYTES:
                _fail("source-wheel archive exceeds its size ceiling")
            digest = hashlib.sha256()
            total = 0
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        _fail("source-wheel archive exceeds its size ceiling")
                    digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise WheelhouseCommandError("source-wheel download failed closed") from exc
    if digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        _fail("source-wheel archive digest differs")


def _validated_member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        _fail("source archive contains an unsafe path")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WheelhousePolicyError("source archive path is not UTF-8") from exc
    path = PurePosixPath(name)
    if (
        len(encoded) > 4096
        or unicodedata.normalize("NFC", name) != name
        or path.is_absolute()
        or name.startswith("/")
        or "//" in name
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        _fail("source archive contains an unsafe path")
    return path


def safe_extract_sdist(archive_path: Path, destination: Path, *, expected_root: str) -> Path:
    if destination.exists() or destination.is_symlink():
        _fail("source extraction destination already exists")
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                _fail("source archive member inventory differs")
            paths: set[str] = set()
            folded: set[str] = set()
            file_paths: set[str] = set()
            total = 0
            validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member in members:
                path = _validated_member_path(
                    member.name.rstrip("/") if member.isdir() else member.name
                )
                canonical = path.as_posix()
                if canonical in paths or canonical.casefold() in folded:
                    _fail("source archive contains duplicate or colliding paths")
                paths.add(canonical)
                folded.add(canonical.casefold())
                if not (member.isdir() or member.isreg()):
                    _fail("source archive link or special member is forbidden")
                if member.isreg():
                    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                        _fail("source archive member exceeds its size ceiling")
                    total += member.size
                    file_paths.add(canonical)
                    if total > MAX_TOTAL_BYTES:
                        _fail("source archive expansion exceeds its size ceiling")
                validated.append((member, path))
            if {path.parts[0] for _, path in validated} != {expected_root}:
                _fail("source archive root differs")
            for _, path in validated:
                for index in range(1, len(path.parts)):
                    if "/".join(path.parts[:index]) in file_paths:
                        _fail("source archive contains a file/directory collision")
            for member, path in sorted(validated, key=lambda item: item[1].as_posix().encode()):
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    _fail("source archive regular member cannot be read")
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                )
                try:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > member.size:
                            _fail("source archive member size differs")
                        offset = 0
                        while offset < len(chunk):
                            offset += os.write(descriptor, chunk[offset:])
                    if copied != member.size:
                        _fail("source archive member size differs")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    except (OSError, tarfile.TarError) as exc:
        raise WheelhousePolicyError("source archive is unavailable or malformed") from exc
    return destination / expected_root


def _toolchain_environment(root: Path, *, epoch: int) -> dict[str, str]:
    root.mkdir(mode=0o700)
    home = root / "home"
    temporary = root / "tmp"
    cache = root / "cache"
    for directory in (home, temporary, cache):
        directory.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PIP_CACHE_DIR": str(cache),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "SOURCE_DATE_EPOCH": str(epoch),
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def _verify_toolchain(package: SourceWheel) -> None:
    if platform.python_version() != package.python_version:
        _fail("source-wheel Python version differs from the reviewed toolchain")
    for name, version in package.build_requirements.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise WheelhousePolicyError("source-wheel build toolchain is incomplete") from exc
        if actual != version:
            _fail("source-wheel build toolchain version differs")


def _run(command: list[str], *, cwd: Path, env: dict[str, str], label: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed executable and reviewed arguments
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        rendered = result.stdout.decode("utf-8", errors="replace")[-4000:]
        raise WheelhouseCommandError(f"{label} failed closed:\n{rendered}")


def prepare_wheelhouse(*, requirements_lock: Path, source_contract: Path, wheelhouse: Path) -> None:
    if wheelhouse.exists() or wheelhouse.is_symlink():
        _fail("wheelhouse destination must not exist")
    if not wheelhouse.parent.is_dir():
        _fail("wheelhouse parent is unavailable")
    contract = load_contract(source_contract)
    locked = load_lock(requirements_lock)
    ordinary = validate_contract_against_lock(contract, locked)
    for package in contract.packages:
        _verify_toolchain(package)
    with tempfile.TemporaryDirectory(
        prefix=".k1s-release-wheelhouse-", dir=wheelhouse.parent
    ) as raw:
        root = Path(raw)
        staging = root / "wheelhouse"
        staging.mkdir(mode=0o700)
        filtered = root / "ordinary-requirements.lock"
        filtered.write_text("\n".join(item.line for item in ordinary) + "\n", encoding="utf-8")
        os.chmod(filtered, 0o600)
        download_env = os.environ.copy()
        for name in tuple(download_env):
            if name.startswith("PIP_"):
                del download_env[name]
        download_env.update(
            {
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
            }
        )
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--no-deps",
                "--require-hashes",
                "--dest",
                str(staging),
                "-r",
                str(filtered),
            ],
            cwd=root,
            env=download_env,
            label="reviewed binary-wheel download",
        )
        for package in contract.packages:
            source_archive = root / package.source_filename
            _download(package.source_url, source_archive, package.source_sha256)
            source_parent = root / f"source-{package.name}"
            source_root = safe_extract_sdist(
                source_archive, source_parent, expected_root=package.source_root
            )
            raw_wheels = root / f"raw-wheel-{package.name}"
            raw_wheels.mkdir(mode=0o700)
            env = _toolchain_environment(
                root / f"build-env-{package.name}", epoch=contract.source_date_epoch
            )
            _run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    str(raw_wheels),
                    str(source_root),
                ],
                cwd=root,
                env=env,
                label="reviewed source-wheel build",
            )
            candidates = list(raw_wheels.iterdir())
            if len(candidates) != 1 or candidates[0].name != package.wheel_filename:
                _fail("source-wheel build output identity differs")
            destination = staging / package.wheel_filename
            normalize_wheel(
                candidates[0],
                destination,
                epoch=contract.source_date_epoch,
                distribution=package.name,
                version=package.version,
            )
            if _sha256(destination) != package.wheel_sha256:
                _fail("canonical source-wheel digest differs")
        verify_wheelhouse(staging, requirements_lock)
        os.chmod(staging, 0o700)
        for item in staging.iterdir():
            os.chmod(item, 0o644)
        os.replace(staging, wheelhouse)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements-lock", required=True, type=Path)
    parser.add_argument("--source-contract", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare_wheelhouse(
            requirements_lock=args.requirements_lock.resolve(strict=True),
            source_contract=args.source_contract.resolve(strict=True),
            wheelhouse=args.wheelhouse.absolute(),
        )
    except (WheelhousePolicyError, WheelhouseCommandError, ValueError) as exc:
        print(f"prepare-release-wheelhouse: {exc}", file=sys.stderr)
        return 1
    print(f"wheelhouse={args.wheelhouse} wheels={len(list(args.wheelhouse.glob('*.whl')))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
