#!/usr/bin/env python3
"""Build the k1s runtime wheel consumed by WorkerBee."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("dist/workerbee-runtime"))
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="k1s-workerbee-runtime-") as tmp:
        build_root = Path(tmp)
        shutil.copytree(
            root / "src" / "ae",
            build_root / "src" / "ae",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".mypy_cache", ".pytest_cache"),
        )
        shutil.copy2(root / "requirements.in", build_root / "requirements.in")
        if (root / "LICENSE").is_file():
            shutil.copy2(root / "LICENSE", build_root / "LICENSE")
        (build_root / "README.md").write_text(
            "# k1s WorkerBee runtime\n\nPackaged k1s runtime for K1S WorkerBee.\n",
            encoding="utf-8",
        )
        (build_root / "pyproject.toml").write_text(_pyproject(version), encoding="utf-8")
        subprocess.run(  # noqa: S603 - invokes the current Python executable intentionally.
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "-w",
                str(out_dir),
                str(build_root),
            ],
            check=True,
        )
    return 0


def _pyproject(version: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "k1s-workerbee-runtime"
version = "{version}"
description = "Packaged k1s runtime for K1S WorkerBee"
readme = "README.md"
requires-python = ">=3.11"
license = {{file = "LICENSE"}}
authors = [{{name = "k1s contributors"}}]
dynamic = ["dependencies"]

[project.scripts]
ae = "ae.cli.__main__:main"
ae-rotate-certs = "ae.cli.rotate_certs:main"
k1s = "ae.kctl.__main__:main"
ae-node = "ae.node.server:main"
ae-gateway = "ae.gateway.__main__:main"
ae-worker-stub = "ae.worker_stub.__main__:main"

[tool.setuptools]
package-dir = {{"" = "src"}}

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"ae.resources" = ["**/*.html", "**/*.md", "**/*.txt", "**/*.sql"]

[tool.setuptools.dynamic]
dependencies = {{file = ["requirements.in"]}}
"""


if __name__ == "__main__":
    raise SystemExit(main())
