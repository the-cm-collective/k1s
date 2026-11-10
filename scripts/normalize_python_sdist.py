#!/usr/bin/env python3
"""Create a canonical, Git-mode-bound sdist without modifying its input."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_EXPANSION_RATIO = 200

GENERATED_FILES = {
    "PKG-INFO",
    "setup.cfg",
    "src/k1s_core.egg-info/PKG-INFO",
    "src/k1s_core.egg-info/SOURCES.txt",
    "src/k1s_core.egg-info/dependency_links.txt",
    "src/k1s_core.egg-info/entry_points.txt",
    "src/k1s_core.egg-info/requires.txt",
    "src/k1s_core.egg-info/top_level.txt",
}


class SdistNormalizationError(ValueError):
    """The input sdist cannot be normalized without weakening policy."""


@dataclass(frozen=True)
class SdistMember:
    path: str
    kind: str
    mode: int
    payload: bytes | None


def _validated_path(name: str, *, directory: bool = False) -> str:
    candidate = name[:-1] if directory and name.endswith("/") else name
    if not candidate or "\x00" in candidate or "\\" in candidate:
        raise SdistNormalizationError("sdist contains an unsafe member path")
    try:
        encoded = candidate.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SdistNormalizationError("sdist member path is not UTF-8") from exc
    path = PurePosixPath(candidate)
    if (
        len(encoded) > 4096
        or unicodedata.normalize("NFC", candidate) != candidate
        or path.is_absolute()
        or candidate.startswith("/")
        or "//" in candidate
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or path.as_posix() != candidate
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        raise SdistNormalizationError("sdist contains an unsafe member path")
    return candidate


def _require_unique_paths(names: list[str], *, files: set[str]) -> None:
    if any(count != 1 for count in Counter(names).values()):
        raise SdistNormalizationError("sdist contains duplicate member paths")
    normalized: set[str] = set()
    casefolded: set[str] = set()
    for name in names:
        nfc = unicodedata.normalize("NFC", name)
        folded = nfc.casefold()
        if nfc in normalized or folded in casefolded:
            raise SdistNormalizationError("sdist contains colliding member paths")
        normalized.add(nfc)
        casefolded.add(folded)
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise SdistNormalizationError("sdist contains a file/directory path collision")


def load_source_modes(path: Path) -> dict[str, int]:
    """Load canonical source-member JSONL and return file modes."""
    result: dict[str, int] = {}
    previous: bytes | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SdistNormalizationError("source member manifest is unreadable") from exc
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SdistNormalizationError("source member manifest is malformed") from exc
        expected_keys = {"gitMode", "mode", "path", "sha256", "size", "type"}
        if type(value) is not dict or set(value) != expected_keys:
            raise SdistNormalizationError("source member manifest schema differs")
        name = _validated_path(str(value["path"]), directory=value["type"] == "directory")
        encoded = name.encode("utf-8")
        if previous is not None and encoded <= previous:
            raise SdistNormalizationError("source member manifest is not strictly ordered")
        previous = encoded
        if name in result:
            raise SdistNormalizationError("source member manifest contains duplicates")
        if value["type"] == "directory":
            if value["gitMode"] is not None or value["mode"] != "0755" or value["size"] != 0:
                raise SdistNormalizationError("source directory manifest identity differs")
            continue
        if value["type"] != "file" or value["gitMode"] not in {"100644", "100755"}:
            raise SdistNormalizationError("source file manifest identity differs")
        expected_mode = "0644" if value["gitMode"] == "100644" else "0755"
        if value["mode"] != expected_mode or not isinstance(value["sha256"], str):
            raise SdistNormalizationError("source file mode identity differs")
        result[name] = int(expected_mode, 8)
    return result


def _read_members(
    path: Path,
    *,
    source_modes: dict[str, int],
    distribution: str,
    version: str,
) -> list[SdistMember]:
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise SdistNormalizationError("sdist input is missing or exceeds its size ceiling")
    expected_filename = f"{distribution.replace('-', '_')}-{version}.tar.gz"
    if path.name != expected_filename:
        raise SdistNormalizationError("sdist filename identity differs")
    root = f"{distribution.replace('-', '_')}-{version}"
    result: list[SdistMember] = []
    total = 0
    compressed_size = path.stat().st_size
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise SdistNormalizationError("sdist member count exceeds its ceiling")
            names: list[str] = []
            files: set[str] = set()
            for member in members:
                name = _validated_path(member.name, directory=member.isdir())
                names.append(name)
                if member.isfile():
                    files.add(name)
            _require_unique_paths(names, files=files)
            for member in members:
                name = _validated_path(member.name, directory=member.isdir())
                member_path = PurePosixPath(name)
                if member_path.parts[:1] != (root,):
                    raise SdistNormalizationError("sdist member is outside its canonical root")
                # Setuptools emits fractional PAX mtime values. They carry no
                # authority and are replaced by the exact policy epoch.
                if set(member.pax_headers) - {"path", "mtime"}:
                    raise SdistNormalizationError("sdist contains unsupported PAX metadata")
                if member.issparse() or not (member.isdir() or member.isfile()):
                    raise SdistNormalizationError(
                        "sdist link, sparse, or special member is forbidden"
                    )
                if member.isdir():
                    result.append(
                        SdistMember(path=name, kind="directory", mode=0o755, payload=None)
                    )
                    continue
                if member.size > MAX_MEMBER_BYTES:
                    raise SdistNormalizationError("sdist member exceeds its size ceiling")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise SdistNormalizationError("sdist expansion exceeds its total ceiling")
                handle = archive.extractfile(member)
                if handle is None:
                    raise SdistNormalizationError("sdist regular member is unreadable")
                payload = handle.read(MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_MEMBER_BYTES:
                    raise SdistNormalizationError("sdist member size differs after extraction")
                relative = PurePosixPath(*member_path.parts[1:]).as_posix()
                if relative in source_modes:
                    mode = source_modes[relative]
                elif relative in GENERATED_FILES:
                    mode = 0o644
                else:
                    raise SdistNormalizationError("sdist contains an unowned generated member")
                result.append(SdistMember(path=name, kind="file", mode=mode, payload=payload))
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError("sdist input is corrupt") from exc
    if compressed_size and total > compressed_size * MAX_EXPANSION_RATIO:
        raise SdistNormalizationError("sdist expansion ratio exceeds its ceiling")
    return sorted(result, key=lambda member: member.path.encode("utf-8"))


def normalize(
    input_path: Path,
    output_path: Path | None = None,
    *,
    epoch: int,
    distribution: str = "k1s-core",
    version: str = "1.0.0a3",
    source_members: Path | None = None,
) -> list[SdistMember]:
    """Normalize *input_path* into a new sdist; in-place operation is forbidden."""
    if output_path is None:
        output_path = input_path.with_name(f"{input_path.name}.normalized")
    if source_members is None:
        raise SdistNormalizationError("source member manifest is required")
    if output_path.exists() or output_path.is_symlink():
        raise SdistNormalizationError("sdist output already exists")
    if not output_path.parent.is_dir():
        raise SdistNormalizationError("sdist output parent is missing")
    source_modes = load_source_modes(source_members)
    members = _read_members(
        input_path,
        source_modes=source_modes,
        distribution=distribution,
        version=version,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        with (
            temporary.open("wb") as raw_output,
            gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=epoch,
            ) as compressed_output,
            tarfile.open(fileobj=compressed_output, mode="w", format=tarfile.PAX_FORMAT) as archive,
        ):
            for member in members:
                info = tarfile.TarInfo(member.path)
                info.mtime = epoch
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = member.mode
                info.linkname = ""
                info.pax_headers = {}
                if member.kind == "directory":
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                    archive.addfile(info)
                else:
                    assert member.payload is not None
                    info.type = tarfile.REGTYPE
                    info.size = len(member.payload)
                    archive.addfile(info, io.BytesIO(member.payload))
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.replace(temporary, output_path)
        directory_fd = os.open(output_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return members


def member_sha256(member: SdistMember) -> str | None:
    if member.payload is None:
        return None
    return hashlib.sha256(member.payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-members", required=True, type=Path)
    args = parser.parse_args()
    try:
        members = normalize(
            args.input,
            args.output,
            epoch=args.epoch,
            distribution=args.distribution,
            version=args.version,
            source_members=args.source_members,
        )
    except SdistNormalizationError as exc:
        parser.exit(5, f"sdist normalization failed closed: {exc}\n")
    print(f"normalized={args.output.name} members={len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
