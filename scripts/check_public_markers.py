#!/usr/bin/env python3
"""Fail closed on private or generated markers in public source and artifacts."""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROTECTED_PATHS = {
    ".env",
    ".envrc",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
    "ops/systemd/ae-ha-core.env",
    "ops/systemd/ae.env",
}
SOURCE_FILES = ("CHANGELOG.md", "README.md", "VERSIONING.md", "pyproject.toml")
SOURCE_DIRS = (
    ".gitea/workflows",
    ".github/workflows",
    "docs",
    "scripts",
    "specs",
    "src/ae",
    "tests",
)
SKIP_DIR_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache", "site"}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    allowed_paths: tuple[re.Pattern[str], ...] = ()


SCANNER_FIXTURES = (
    re.compile(r"scripts/check_public_markers\.py"),
    re.compile(r"tests/integration/test_docs_export_and_links\.py"),
    re.compile(r"tests/unit/test_python_artifact_install_contract\.py"),
    re.compile(r"tests/unit/test_public_identity_contract\.py"),
)
GITEA_FIXTURES = (
    re.compile(
        r"\.gitea/workflows/(?:ci-core|ci-docs|nightly-apishim|nightly-runtime|release)\.yml"
    ),
    re.compile(r"scripts/ci/lib\.sh"),
    re.compile(r"tests/unit/test_release_foundation_contract\.py"),
    *SCANNER_FIXTURES,
)
RULES = (
    Rule("internal-gitea", re.compile(r"gitea\.core\.home\.arpa", re.IGNORECASE), GITEA_FIXTURES),
    Rule(
        "private-home",
        re.compile(r"/home/(?!ae(?:/|$)|<you>(?:/|$)|\$USER(?:/|$))[\w.-]+/(?:git|\.ssh)(?:/|$)"),
        SCANNER_FIXTURES,
    ),
    Rule(
        "private-checkout",
        re.compile(r"(?:^|[/\s])(?:k1s-private|k1s-workerbee-private)(?:[/\s]|$)", re.IGNORECASE),
        SCANNER_FIXTURES,
    ),
    Rule(
        "credential-material",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r"|\bgh[pousr]_[A-Za-z0-9_]{20,}\b"
            r"|\bAKIA[0-9A-Z]{16}\b"
        ),
        SCANNER_FIXTURES,
    ),
    Rule(
        "generated-evidence",
        re.compile(
            r"/tmp/k1s-(?:rc1|fixed-lane|core-(?:artifact|release))[-\w.]*",  # noqa: S108
            re.IGNORECASE,
        ),
        SCANNER_FIXTURES,
    ),
    Rule(
        "legacy-codeberg-default",
        re.compile(r"codeberg\.org/th3_4rchit3ct/k1s|Mirror \(Codeberg\)", re.IGNORECASE),
        (re.compile(r"CHANGELOG\.md"), *SCANNER_FIXTURES),
    ),
)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int


def _normalized_member(name: str) -> str:
    parts = PurePosixPath(name).parts
    if parts and re.fullmatch(r"k1s[_-]core-[^/]+", parts[0], re.IGNORECASE):
        parts = parts[1:]
    return PurePosixPath(*parts).as_posix()


def _is_allowed(rule: Rule, path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in rule.allowed_paths)


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            if rule.pattern.search(line) and not _is_allowed(rule, path):
                findings.append(Finding(rule.name, path, line_number))
    return findings


def _source_paths(root: Path) -> Iterator[Path]:
    for relative in SOURCE_FILES:
        path = root / relative
        if path.is_file():
            yield path
    for relative in SOURCE_DIRS:
        base = root / relative
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts):
                continue
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def scan_source(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _source_paths(root):
        relative = path.relative_to(root).as_posix()
        if relative in PROTECTED_PATHS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(relative, text))
    return findings


def _archive_texts(path: Path) -> Iterable[tuple[str, bytes]]:
    if path.suffix == ".whl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                logical = _normalized_member(name)
                if Path(logical).suffix.lower() in TEXT_SUFFIXES:
                    yield logical, archive.read(name)
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                logical = _normalized_member(member.name)
                if Path(logical).suffix.lower() not in TEXT_SUFFIXES:
                    continue
                payload = archive.extractfile(member)
                if payload is not None:
                    yield logical, payload.read()
        return
    raise ValueError(f"unsupported artifact format: {path}")


def scan_archive(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for logical, payload in _archive_texts(path):
        if logical in PROTECTED_PATHS:
            findings.append(Finding("protected-path", logical, 0))
            continue
        findings.extend(scan_text(logical, payload.decode("utf-8", errors="replace")))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--archive", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.source_root is None and not args.archive:
        parser.error("at least one --source-root or --archive is required")

    findings: list[Finding] = []
    if args.source_root is not None:
        findings.extend(scan_source(args.source_root.resolve()))
    for archive in args.archive:
        findings.extend(scan_archive(archive.resolve()))
    if findings:
        for finding in findings:
            print(
                f"public-marker: {finding.rule}: {finding.path}:{finding.line}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
