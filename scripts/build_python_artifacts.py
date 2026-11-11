#!/usr/bin/env python3
"""Build and verify canonical k1s-core artifacts from an exact Git tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

try:
    from scripts.normalize_python_sdist import (
        SdistNormalizationError,
    )
    from scripts.normalize_python_sdist import (
        normalize as normalize_sdist,
    )
    from scripts.normalize_python_wheel import (
        WheelNormalizationError,
    )
    from scripts.normalize_python_wheel import (
        normalize as normalize_wheel,
    )
    from scripts.project_version import read_project_version
    from scripts.validate_installed_python_artifact import verify_wheelhouse
    from scripts.validate_python_artifacts import (
        CANONICAL_SOURCE_DATE_EPOCH,
        validate,
        verify_bundle_metadata,
        write_bundle_metadata,
    )
except ModuleNotFoundError:  # direct execution from scripts/
    from normalize_python_sdist import (  # type: ignore[import-not-found,no-redef]
        SdistNormalizationError,
    )
    from normalize_python_sdist import (  # type: ignore[no-redef]
        normalize as normalize_sdist,
    )
    from normalize_python_wheel import (  # type: ignore[import-not-found,no-redef]
        WheelNormalizationError,
    )
    from normalize_python_wheel import (  # type: ignore[no-redef]
        normalize as normalize_wheel,
    )
    from project_version import read_project_version  # type: ignore[import-not-found,no-redef]
    from validate_installed_python_artifact import (  # type: ignore[import-not-found,no-redef]
        verify_wheelhouse,
    )
    from validate_python_artifacts import (  # type: ignore[import-not-found,no-redef]
        CANONICAL_SOURCE_DATE_EPOCH,
        validate,
        verify_bundle_metadata,
        write_bundle_metadata,
    )

DIST_NAME = "k1s-core"
PROTECTED_PATHS = (
    ".env",
    ".envrc",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    "ops/systemd/ae-ha-core.env",
    "ops/systemd/ae.env",
)
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_ENTRIES = 25_000
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
FULL_OBJECT = re.compile(r"[0-9a-f]{40}")


class BuildPolicyError(ValueError):
    """The requested build cannot proceed within the frozen policy."""


class BuildCommandError(RuntimeError):
    """A bounded build or validation command failed."""


class InstalledValidationError(RuntimeError):
    """The strict installed-artifact gate failed."""


@dataclass(frozen=True)
class GitFile:
    path: str
    mode: str
    oid: str
    payload: bytes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_path(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise BuildPolicyError("Git tree contains an unsafe path")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BuildPolicyError("Git tree path is not UTF-8") from exc
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
        raise BuildPolicyError("Git tree path is not canonical")
    return name


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _git(
    git: Path,
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    result = subprocess.run(  # noqa: S603 - executable is an explicit reviewed input
        [str(git), "-C", str(repository), *arguments],
        input=input_bytes,
        capture_output=True,
        env=_git_environment(),
        check=False,
    )
    if result.returncode not in allowed_returncodes:
        raise BuildPolicyError("Git object operation failed closed")
    return result.stdout


def _validate_identity(
    *,
    git: Path,
    repository: Path,
    commit: str,
    tree: str,
) -> None:
    if FULL_OBJECT.fullmatch(commit) is None or FULL_OBJECT.fullmatch(tree) is None:
        raise BuildPolicyError("commit and tree must be full lowercase object IDs")
    if not git.is_absolute() or not git.is_file() or not os.access(git, os.X_OK):
        raise BuildPolicyError("explicit Git executable identity is invalid")
    if not repository.is_absolute() or not repository.is_dir():
        raise BuildPolicyError("repository object source is invalid")
    replacements = _git(git, repository, "for-each-ref", "--format=%(refname)", "refs/replace")
    if replacements:
        raise BuildPolicyError("Git replacement refs are forbidden")
    partial = _git(
        git,
        repository,
        "config",
        "--local",
        "--get-regexp",
        r"^remote\..*\.promisor$",
        allowed_returncodes=(0, 1),
    )
    if partial.strip():
        raise BuildPolicyError("promisor repositories are forbidden")
    actual_commit = (
        _git(git, repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    )
    actual_tree = (
        _git(git, repository, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    )
    if actual_commit != commit or actual_tree != tree:
        raise BuildPolicyError("commit/tree identity differs")


def _tree_listing(*, git: Path, repository: Path, commit: str) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    paths: set[str] = set()
    folded: set[str] = set()

    def add_records(raw: bytes) -> None:
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                metadata, encoded_path = record.split(b"\t", 1)
                mode, object_type, oid = metadata.decode("ascii").split(" ")
                path = _validate_path(encoded_path.decode("utf-8", errors="strict"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise BuildPolicyError("Git tree listing is malformed") from exc
            if path in PROTECTED_PATHS:
                raise BuildPolicyError("protected path entered the safe Git listing")
            if object_type != "blob" or mode not in {"100644", "100755"}:
                raise BuildPolicyError("Git tree contains an unsupported mode or type")
            canonical = unicodedata.normalize("NFC", path)
            if path in paths or canonical.casefold() in folded:
                raise BuildPolicyError("Git tree contains a path collision")
            paths.add(path)
            folded.add(canonical.casefold())
            result.append((path, mode, oid))

    def has_protected_descendant(prefix: str) -> bool:
        needle = f"{prefix}/" if prefix else ""
        return any(path.startswith(needle) for path in PROTECTED_PATHS)

    def walk(prefix: str) -> None:
        if prefix and not has_protected_descendant(prefix):
            add_records(
                _git(
                    git,
                    repository,
                    "ls-tree",
                    "-r",
                    "-z",
                    "--full-tree",
                    commit,
                    "--",
                    prefix,
                )
            )
            return
        treeish = commit if not prefix else f"{commit}:{prefix}"
        raw_names = _git(git, repository, "ls-tree", "-z", "--name-only", treeish)
        for encoded_name in raw_names.split(b"\0"):
            if not encoded_name:
                continue
            try:
                child_name = encoded_name.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BuildPolicyError("Git tree name is not UTF-8") from exc
            child = _validate_path(f"{prefix}/{child_name}" if prefix else child_name)
            if child in PROTECTED_PATHS:
                continue
            raw_entry = _git(git, repository, "ls-tree", "-z", commit, "--", child)
            record = raw_entry.removesuffix(b"\0")
            try:
                metadata, encoded_path = record.split(b"\t", 1)
                mode, object_type, oid = metadata.decode("ascii").split(" ")
                returned_path = encoded_path.decode("utf-8", errors="strict")
            except (ValueError, UnicodeDecodeError) as exc:
                raise BuildPolicyError("Git tree entry is malformed") from exc
            if returned_path != child:
                raise BuildPolicyError("Git tree entry path differs")
            if object_type == "tree" and mode == "040000":
                walk(child)
            elif object_type == "blob" and mode in {"100644", "100755"}:
                add_records(raw_entry)
            else:
                raise BuildPolicyError("Git tree contains an unsupported mode or type")

    walk("")
    if len(result) > MAX_SOURCE_FILES:
        raise BuildPolicyError("Git tree exceeds its file-count ceiling")
    file_paths = {path for path, _, _ in result}
    for path in file_paths:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in file_paths:
                raise BuildPolicyError("Git tree contains a file/directory collision")
    return sorted(result, key=lambda value: value[0].encode("utf-8"))


def _cat_blobs(
    *,
    git: Path,
    repository: Path,
    listing: list[tuple[str, str, str]],
) -> list[GitFile]:
    query = b"".join(f"{oid}\n".encode("ascii") for _, _, oid in listing)
    raw = _git(git, repository, "cat-file", "--batch", input_bytes=query)
    offset = 0
    total = 0
    result: list[GitFile] = []
    for path, mode, expected_oid in listing:
        end = raw.find(b"\n", offset)
        if end < 0:
            raise BuildPolicyError("Git blob batch response is truncated")
        try:
            oid, object_type, encoded_size = raw[offset:end].decode("ascii").split(" ")
            size = int(encoded_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BuildPolicyError("Git blob batch response is malformed") from exc
        offset = end + 1
        if oid != expected_oid or object_type != "blob" or size < 0 or size > MAX_SOURCE_FILE_BYTES:
            raise BuildPolicyError("Git blob identity or size differs")
        payload = raw[offset : offset + size]
        offset += size
        if len(payload) != size or raw[offset : offset + 1] != b"\n":
            raise BuildPolicyError("Git blob batch payload is malformed")
        offset += 1
        total += size
        if total > MAX_SOURCE_BYTES:
            raise BuildPolicyError("Git tree exceeds its byte ceiling")
        result.append(GitFile(path=path, mode=mode, oid=oid, payload=payload))
    if offset != len(raw):
        raise BuildPolicyError("Git blob batch contains unexpected trailing data")
    return result


def _safe_create_file(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_rows(files: list[GitFile]) -> list[dict[str, object]]:
    directories: set[str] = set()
    for file in files:
        parts = PurePosixPath(file.path).parts
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if len(files) + len(directories) > MAX_SOURCE_ENTRIES:
        raise BuildPolicyError("Git tree exceeds its total-entry ceiling")
    rows: list[dict[str, object]] = []
    for directory in directories:
        rows.append(
            {
                "gitMode": None,
                "mode": "0755",
                "path": directory,
                "sha256": None,
                "size": 0,
                "type": "directory",
            }
        )
    for file in files:
        rows.append(
            {
                "gitMode": file.mode,
                "mode": "0644" if file.mode == "100644" else "0755",
                "path": file.path,
                "sha256": hashlib.sha256(file.payload).hexdigest(),
                "size": len(file.payload),
                "type": "file",
            }
        )
    return sorted(rows, key=lambda value: str(value["path"]).encode("utf-8"))


def export_source(
    *,
    git: Path,
    repository: Path,
    commit: str,
    tree: str,
    source_root: Path,
    source_manifest: Path,
) -> list[dict[str, object]]:
    """Export an exact safe Git tree using only object modes and bytes."""
    _validate_identity(git=git, repository=repository, commit=commit, tree=tree)
    files = _cat_blobs(
        git=git,
        repository=repository,
        listing=_tree_listing(git=git, repository=repository, commit=commit),
    )
    rows = _source_rows(files)
    source_root.mkdir(mode=0o755)
    for row in rows:
        if row["type"] == "directory":
            directory = source_root / str(row["path"])
            directory.mkdir(mode=0o755)
            os.chmod(directory, 0o755, follow_symlinks=False)  # noqa: S103
    by_path = {file.path: file for file in files}
    for row in rows:
        if row["type"] != "file":
            continue
        file = by_path[str(row["path"])]
        _safe_create_file(
            source_root / file.path,
            file.payload,
            0o644 if file.mode == "100644" else 0o755,
        )
    source_manifest.parent.mkdir(mode=0o755, exist_ok=True)
    source_manifest.write_bytes(b"".join(_canonical_json(row) for row in rows))
    os.chmod(source_manifest, 0o644)
    return rows


def _private_parent(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise BuildPolicyError("output root must be an absent absolute path")
    parent = path.parent
    details = parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise BuildPolicyError("output parent identity is unsafe")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise BuildPolicyError("output parent must be owner-private mode 0700")
    path.mkdir(mode=0o700)


def _socket_guard(path: Path) -> None:
    path.mkdir(mode=0o700)
    payload = b"""import socket

def _deny(*args, **kwargs):
    raise RuntimeError("external network disabled by k1s artifact builder")

_original_socket = socket.socket
class GuardedSocket(_original_socket):
    def __init__(self, family=socket.AF_INET, *args, **kwargs):
        if family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("external network disabled by k1s artifact builder")
        super().__init__(family, *args, **kwargs)
    def connect(self, address):
        raise RuntimeError("socket connect disabled by k1s artifact builder")

socket.socket = GuardedSocket
socket.create_connection = _deny
"""
    _safe_create_file(path / "sitecustomize.py", payload, 0o600)


def _build_environment(*, root: Path, guard: Path, python: Path, git: Path) -> dict[str, str]:
    root.mkdir(mode=0o700)
    home = root / "home"
    temporary = root / "tmp"
    cache = root / "cache"
    for directory in (home, temporary, cache):
        directory.mkdir(mode=0o700)
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join((str(python.parent), str(git.parent), "/usr/bin", "/bin")),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(guard),
        "SOURCE_DATE_EPOCH": str(CANONICAL_SOURCE_DATE_EPOCH),
        "TMPDIR": str(temporary),
        "TZ": "UTC",
        "XDG_CACHE_HOME": str(cache),
    }


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    installed: bool = False,
) -> None:
    result = subprocess.run(  # noqa: S603 - command is a fixed internal argv
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rendered = result.stdout.decode("utf-8", errors="replace")
    rendered = re.sub(r"/(?:home|tmp)/[^\s'\"<>]+", "<private-path>", rendered)
    log.write_text(rendered, encoding="utf-8")
    os.chmod(log, 0o600)
    if result.returncode:
        if installed:
            raise InstalledValidationError("strict installed-artifact validation failed")
        raise BuildCommandError("bounded artifact command failed")


def _distribution_identity(python: Path, env: dict[str, str]) -> dict[str, object]:
    packages = ("build", "setuptools", "wheel", "twine", "packaging", "pyproject-hooks")
    script = (
        "import hashlib,importlib.metadata,json,pathlib; out={};"
        "\nfor name in " + repr(packages) + ":"
        "\n d=importlib.metadata.distribution(name); h=hashlib.sha256();"
        "\n files=sorted((d.files or []),key=lambda p:str(p));"
        "\n for f in files:"
        "\n  p=d.locate_file(f);"
        "\n  if p.is_file(): h.update(str(f).encode()+b'\\0'+p.read_bytes())"
        "\n out[name]={'version':d.version,'recordDigest':h.hexdigest()}"
        "\nprint(json.dumps(out,sort_keys=True,separators=(',',':')))"
    )
    result = subprocess.run(  # noqa: S603 - executable is an explicit reviewed input
        [str(python), "-c", script], env=env, capture_output=True
    )
    if result.returncode:
        raise BuildCommandError("reviewed build toolchain is incomplete")
    return cast(dict[str, object], json.loads(result.stdout))


def _executable_identity(path: Path, version: str) -> dict[str, object]:
    return {"sha256": _sha256(path), "size": path.stat().st_size, "version": version}


def _tool_inputs(
    *, python: Path, git: Path, env: dict[str, str], wheelhouse: Path, lock: Path
) -> dict[str, object]:
    python_version = subprocess.run(  # noqa: S603 - explicit reviewed executable
        [str(python), "--version"], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    git_version = subprocess.run(  # noqa: S603 - explicit reviewed executable
        [str(git), "--version"], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    wheels = [
        {"filename": path.name, "sha256": _sha256(path), "size": path.stat().st_size}
        for path in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name.encode())
    ]
    if len(wheels) != 24:
        raise BuildPolicyError("runtime wheelhouse must contain exactly 24 wheels")
    stream = b"".join(_canonical_json(item) for item in wheels)
    return {
        "buildDistributions": _distribution_identity(python, env),
        "git": _executable_identity(git, git_version),
        "python": _executable_identity(python, python_version),
        "requirementsLockSha256": _sha256(lock),
        "runtimeWheelhouseCount": len(wheels),
        "runtimeWheelhouseIdentity": hashlib.sha256(stream).hexdigest(),
        "runtimeWheels": wheels,
    }


def _resolve_raw_artifacts(raw: Path, *, version: str) -> tuple[Path, Path]:
    wheel = raw / f"k1s_core-{version}-py3-none-any.whl"
    sdist = raw / f"k1s_core-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file() or set(raw.iterdir()) != {wheel, sdist}:
        raise BuildCommandError("raw build output identity differs")
    return wheel, sdist


def build(args: argparse.Namespace) -> dict[str, object]:
    repository = args.repository
    output_root = args.output_root
    _private_parent(output_root)
    try:
        work = output_root / "work"
        bundle = output_root / ".bundle.pending"
        logs = output_root / "logs"
        for directory in (work, bundle, logs):
            directory.mkdir(mode=0o700)
        source = work / "source"
        manifests = bundle / "manifests"
        manifests.mkdir(mode=0o755)
        source_manifest = manifests / "source-members.jsonl"
        export_source(
            git=args.git,
            repository=repository,
            commit=args.commit,
            tree=args.tree,
            source_root=source,
            source_manifest=source_manifest,
        )
        version = read_project_version(source / "pyproject.toml")
        if version != "1.0.0":
            raise BuildPolicyError("project version differs from the frozen release identity")
        if args.source_date_epoch != CANONICAL_SOURCE_DATE_EPOCH:
            raise BuildPolicyError("SOURCE_DATE_EPOCH differs from policy")
        verify_wheelhouse(args.runtime_wheelhouse, args.requirements_lock)
        if not args.python.is_absolute() or not args.python.is_file():
            raise BuildPolicyError("explicit Python executable identity is invalid")
        guard = work / "guard"
        _socket_guard(guard)
        env = _build_environment(
            root=work / "environment", guard=guard, python=args.python, git=args.git
        )
        raw = work / "raw"
        raw.mkdir(mode=0o700)
        _run(
            [
                str(args.python),
                "-m",
                "build",
                "--no-isolation",
                "--sdist",
                "--wheel",
                "--outdir",
                str(raw),
            ],
            cwd=source,
            env=env,
            log=logs / "build.log",
        )
        raw_wheel, raw_sdist = _resolve_raw_artifacts(raw, version=version)
        dist = bundle / "dist"
        dist.mkdir(mode=0o755)
        wheel = dist / raw_wheel.name
        sdist = dist / raw_sdist.name
        normalize_wheel(
            raw_wheel,
            wheel,
            epoch=CANONICAL_SOURCE_DATE_EPOCH,
            distribution=DIST_NAME,
            version=version,
        )
        normalize_sdist(
            raw_sdist,
            sdist,
            epoch=CANONICAL_SOURCE_DATE_EPOCH,
            distribution=DIST_NAME,
            version=version,
            source_members=source_manifest,
        )
        validate(
            dist,
            pyproject=source / "pyproject.toml",
            source_members=source_manifest,
            epoch=CANONICAL_SOURCE_DATE_EPOCH,
        )
        _run(
            [str(args.python), "-m", "twine", "check", str(wheel), str(sdist)],
            cwd=source,
            env=env,
            log=logs / "twine.log",
        )
        install_root = work / "installed-smoke"
        _run(
            [
                str(args.python),
                str(source / "scripts" / "validate_installed_python_artifact.py"),
                "--wheel",
                str(wheel),
                "--wheelhouse",
                str(args.runtime_wheelhouse),
                "--requirements-lock",
                str(args.requirements_lock),
                "--work-root",
                str(install_root),
                "--expected-version",
                version,
            ],
            cwd=source,
            env=env,
            log=logs / "installed.log",
            installed=True,
        )
        tool_inputs = _tool_inputs(
            python=args.python,
            git=args.git,
            env=env,
            wheelhouse=args.runtime_wheelhouse,
            lock=args.requirements_lock,
        )
        artifact_manifest = bundle / "artifact-manifest.json"
        sha256sums = bundle / "SHA256SUMS"
        value = write_bundle_metadata(
            dist=dist,
            source_members=source_manifest,
            wheel_members=manifests / "wheel-members.jsonl",
            sdist_members=manifests / "sdist-members.jsonl",
            artifact_manifest=artifact_manifest,
            sha256sums=sha256sums,
            commit=args.commit,
            tree=args.tree,
            tool_inputs=tool_inputs,
            validation={
                "artifactValidator": "passed",
                "installedArtifact": "passed",
                "publicMarkers": "passed",
                "twine": "passed",
            },
        )
        verify_bundle_metadata(bundle=bundle, expected_commit=args.commit, expected_tree=args.tree)
        final_bundle = output_root / "bundle"
        os.replace(bundle, final_bundle)
        output_fd = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        shutil.rmtree(work)
        return {
            "artifactManifestSha256": _sha256(final_bundle / "artifact-manifest.json"),
            "artifacts": value["artifacts"],
            "commit": args.commit,
            "sha256sumsSha256": _sha256(final_bundle / "SHA256SUMS"),
            "status": "built-and-verified",
            "tree": args.tree,
        }
    except BaseException:
        raise


def verify(args: argparse.Namespace) -> dict[str, object]:
    _validate_identity(
        git=args.git,
        repository=args.repository,
        commit=args.commit,
        tree=args.tree,
    )
    value = verify_bundle_metadata(
        bundle=args.bundle_root,
        expected_commit=args.commit,
        expected_tree=args.tree,
    )
    with tempfile.TemporaryDirectory(prefix="k1s-artifact-verify-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        source_manifest = root / "source-members.jsonl"
        export_source(
            git=args.git,
            repository=args.repository,
            commit=args.commit,
            tree=args.tree,
            source_root=root / "source",
            source_manifest=source_manifest,
        )
        bundle_source_members = args.bundle_root / "manifests/source-members.jsonl"
        if source_manifest.read_bytes() != bundle_source_members.read_bytes():
            raise BuildPolicyError("bundle source manifest differs from exact Git tree")
        validate(
            args.bundle_root / "dist",
            pyproject=root / "source/pyproject.toml",
            source_members=source_manifest,
        )
    return {
        "artifactManifestSha256": _sha256(args.bundle_root / "artifact-manifest.json"),
        "artifacts": value["artifacts"],
        "commit": args.commit,
        "sha256sumsSha256": _sha256(args.bundle_root / "SHA256SUMS"),
        "status": "verified",
        "tree": args.tree,
    }


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--repository", required=True, type=_absolute_path)
        subparser.add_argument("--commit", required=True)
        subparser.add_argument("--tree", required=True)
        subparser.add_argument("--git", required=True, type=_absolute_path)
        subparser.add_argument("--python", required=True, type=_absolute_path)
        subparser.add_argument(
            "--source-date-epoch", required=True, type=int, choices=[CANONICAL_SOURCE_DATE_EPOCH]
        )
    build_parser = subparsers.choices["build"]
    build_parser.add_argument("--runtime-wheelhouse", required=True, type=_absolute_path)
    build_parser.add_argument("--requirements-lock", required=True, type=_absolute_path)
    build_parser.add_argument("--output-root", required=True, type=_absolute_path)
    verify_parser = subparsers.choices["verify"]
    verify_parser.add_argument("--bundle-root", required=True, type=_absolute_path)
    return parser


def _exit(parser: argparse.ArgumentParser, code: int, message: str) -> NoReturn:
    parser.exit(code, f"artifact construction failed closed: {message}\n")


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        value = build(args) if args.command == "build" else verify(args)
    except BuildPolicyError as exc:
        _exit(parser, 3, str(exc))
    except BuildCommandError as exc:
        _exit(parser, 4, str(exc))
    except (WheelNormalizationError, SdistNormalizationError, ValueError) as exc:
        _exit(parser, 5, str(exc))
    except InstalledValidationError as exc:
        _exit(parser, 6, str(exc))
    except Exception:
        _exit(parser, 70, "unexpected internal failure")
    sys.stdout.buffer.write(_canonical_json(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
