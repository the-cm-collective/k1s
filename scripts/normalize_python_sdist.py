#!/usr/bin/env python3
"""Normalize sdist container metadata for exact-byte reproducibility."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import tarfile
from pathlib import Path


def normalize(path: Path, *, epoch: int) -> None:
    temporary = path.with_name(f".{path.name}.normalized")
    with (
        tarfile.open(path, "r:gz") as source,
        temporary.open("wb") as raw_output,
        gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_output,
            mtime=epoch,
        ) as compressed_output,
        tarfile.open(
            fileobj=compressed_output,
            mode="w",
            format=tarfile.PAX_FORMAT,
        ) as destination,
    ):
        for member in source.getmembers():
            normalized = copy.copy(member)
            normalized.mtime = epoch
            normalized.uid = 0
            normalized.gid = 0
            normalized.uname = ""
            normalized.gname = ""
            normalized.pax_headers = {}
            payload = source.extractfile(member) if member.isfile() else None
            destination.addfile(normalized, payload)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("sdists", nargs="+", type=Path)
    args = parser.parse_args()
    for sdist in args.sdists:
        normalize(sdist, epoch=args.epoch)
        print(f"normalized={sdist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
