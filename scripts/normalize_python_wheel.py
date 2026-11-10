#!/usr/bin/env python3
"""Create a canonical, policy-checked wheel without modifying its input."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import stat
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_EXPANSION_RATIO = 200
CANONICAL_FILE_MODE = stat.S_IFREG | 0o644


class WheelNormalizationError(ValueError):
    """The input wheel cannot be normalized without weakening policy."""


@dataclass(frozen=True)
class WheelMember:
    path: str
    payload: bytes


def _validated_path(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.endswith("/"):
        raise WheelNormalizationError("wheel contains an unsafe member path")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WheelNormalizationError("wheel member path is not UTF-8") from exc
    if len(encoded) > 4096 or unicodedata.normalize("NFC", name) != name:
        raise WheelNormalizationError("wheel member path is not canonical Unicode")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or name.startswith("/")
        or "//" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        or (path.parts and ":" in path.parts[0])
        or path.as_posix() != name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise WheelNormalizationError("wheel contains an unsafe member path")
    return name


def _require_unique_paths(names: list[str]) -> None:
    duplicates = [name for name, count in Counter(names).items() if count != 1]
    if duplicates:
        raise WheelNormalizationError("wheel contains duplicate member paths")
    normalized: set[str] = set()
    casefolded: set[str] = set()
    files = set(names)
    for name in names:
        nfc = unicodedata.normalize("NFC", name)
        folded = nfc.casefold()
        if nfc in normalized or folded in casefolded:
            raise WheelNormalizationError("wheel contains colliding member paths")
        normalized.add(nfc)
        casefolded.add(folded)
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise WheelNormalizationError("wheel contains a file/directory path collision")


def _sha256_record(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _canonical_record(payloads: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted((name for name in payloads if name != record_name), key=str.encode):
        payload = payloads[name]
        writer.writerow((name, _sha256_record(payload), str(len(payload))))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _validate_input_record(payloads: dict[str, bytes], record_name: str) -> None:
    if record_name not in payloads:
        raise WheelNormalizationError("wheel is missing RECORD")
    try:
        rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise WheelNormalizationError("wheel RECORD is malformed") from exc
    if any(len(row) != 3 for row in rows):
        raise WheelNormalizationError("wheel RECORD is malformed")
    names = [row[0] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(payloads):
        raise WheelNormalizationError("wheel RECORD coverage is incomplete or duplicated")
    for name, encoded_hash, encoded_size in rows:
        _validated_path(name)
        if name == record_name:
            if encoded_hash or encoded_size:
                raise WheelNormalizationError("wheel RECORD self-row is not empty")
            continue
        payload = payloads[name]
        if encoded_hash != _sha256_record(payload) or encoded_size != str(len(payload)):
            raise WheelNormalizationError("wheel RECORD hash or size differs")


def _read_members(path: Path, *, distribution: str, version: str) -> tuple[dict[str, bytes], str]:
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise WheelNormalizationError("wheel input is missing or exceeds its size ceiling")
    expected_filename = f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    if path.name != expected_filename:
        raise WheelNormalizationError("wheel filename identity differs")
    payloads: dict[str, bytes] = {}
    compressed_total = 0
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.comment:
                raise WheelNormalizationError("wheel archive comment is forbidden")
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise WheelNormalizationError("wheel member count exceeds its ceiling")
            names = [_validated_path(info.filename) for info in infos]
            _require_unique_paths(names)
            for info in infos:
                if info.is_dir() or info.flag_bits & 0x1:
                    raise WheelNormalizationError(
                        "wheel directories and encrypted members are forbidden"
                    )
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise WheelNormalizationError("wheel compression method is unsupported")
                mode_type = stat.S_IFMT(info.external_attr >> 16)
                if mode_type not in {0, stat.S_IFREG}:
                    raise WheelNormalizationError("wheel link or special member is forbidden")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise WheelNormalizationError("wheel member exceeds its size ceiling")
                total += info.file_size
                compressed_total += info.compress_size
                if total > MAX_TOTAL_BYTES:
                    raise WheelNormalizationError("wheel expansion exceeds its total ceiling")
                payload = archive.read(info)
                if len(payload) != info.file_size:
                    raise WheelNormalizationError("wheel member size differs after extraction")
                payloads[info.filename] = payload
            archive.testzip()
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise WheelNormalizationError("wheel input is corrupt") from exc
    if compressed_total and total > compressed_total * MAX_EXPANSION_RATIO:
        raise WheelNormalizationError("wheel expansion ratio exceeds its ceiling")
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    roots = {
        PurePosixPath(name).parts[0] for name in payloads if name.endswith(".dist-info/RECORD")
    }
    if roots != {dist_info}:
        raise WheelNormalizationError("wheel distribution metadata identity differs")
    record_name = f"{dist_info}/RECORD"
    if f"{dist_info}/RECORD.jws" in payloads or f"{dist_info}/RECORD.p7s" in payloads:
        raise WheelNormalizationError("signed wheel RECORD variants are unsupported")
    _validate_input_record(payloads, record_name)
    return payloads, record_name


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(epoch, tz=UTC)
    if value.year < 1980 or value.year > 2107 or value.second % 2:
        raise WheelNormalizationError("epoch cannot be represented canonically by ZIP")
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def normalize(
    input_path: Path,
    output_path: Path,
    *,
    epoch: int,
    distribution: str,
    version: str,
) -> list[WheelMember]:
    """Normalize *input_path* into a new, canonical wheel at *output_path*."""
    if output_path.exists() or output_path.is_symlink():
        raise WheelNormalizationError("wheel output already exists")
    if not output_path.parent.is_dir():
        raise WheelNormalizationError("wheel output parent is missing")
    payloads, record_name = _read_members(
        input_path,
        distribution=distribution,
        version=version,
    )
    payloads[record_name] = _canonical_record(payloads, record_name)
    names = sorted(payloads, key=str.encode)
    timestamp = _zip_datetime(epoch)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for name in names:
                info = zipfile.ZipInfo(name, date_time=timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = CANONICAL_FILE_MODE << 16
                info.internal_attr = 0
                info.extra = b""
                info.comment = b""
                archive.writestr(
                    info,
                    payloads[name],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
        directory_fd = os.open(output_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return [WheelMember(path=name, payload=payloads[name]) for name in names]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        members = normalize(
            args.input,
            args.output,
            epoch=args.epoch,
            distribution=args.distribution,
            version=args.version,
        )
    except WheelNormalizationError as exc:
        parser.exit(5, f"wheel normalization failed closed: {exc}\n")
    print(f"normalized={args.output.name} members={len(members)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
