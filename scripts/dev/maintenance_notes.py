#!/usr/bin/env python3
"""Refresh/check source-local Maintenance Notes sections for src/ae docs."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MAINTENANCE_HEADER = "## Maintenance Notes"
MAINTENANCE_TERMS = ("deprecated", "legacy", "todo", "fixme", "workaround")
FALLBACK_CONTEXT_TERMS = (
    "invalid",
    "missing",
    "failed",
    "failure",
    "unavailable",
    "unresolved",
    "without",
    "fall back",
    "fallback to",
    "fallback:",
)


@dataclass(frozen=True)
class MaintenanceMarker:
    path: Path
    line_number: int
    text: str
    term: str


def _slug(name: str) -> str:
    return name.replace("_", "-")


def _line_term(line: str) -> str | None:
    lower = line.lower()
    for term in MAINTENANCE_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lower):
            return term.upper() if term in {"todo", "fixme"} else term
    if re.search(r"\bfallback\b", lower) and any(term in lower for term in FALLBACK_CONTEXT_TERMS):
        return "fallback"
    return None


def scan_source_file(path: Path) -> list[MaintenanceMarker]:
    markers: list[MaintenanceMarker] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return markers
    for idx, line in enumerate(lines, start=1):
        term = _line_term(line)
        if term is None:
            continue
        markers.append(
            MaintenanceMarker(
                path=path,
                line_number=idx,
                text=line.strip(),
                term=term,
            )
        )
    return markers


def scan_tree(root: Path) -> dict[Path, list[MaintenanceMarker]]:
    out: dict[Path, list[MaintenanceMarker]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out[path] = scan_source_file(path)
    return out


def doc_path_for_source(root: Path, source: Path) -> Path:
    rel = source.relative_to(root)
    doc_name = f"{_slug(source.stem)}.md"
    if len(rel.parts) == 1:
        return root / "docs" / doc_name
    return root / rel.parts[0] / "docs" / doc_name


def readme_path_for_source(root: Path, source: Path) -> Path:
    rel = source.relative_to(root)
    if len(rel.parts) == 1:
        return root / "README.md"
    return root / rel.parts[0] / "README.md"


def render_module_section(markers: list[MaintenanceMarker]) -> str:
    lines = [MAINTENANCE_HEADER]
    if not markers:
        lines.append(
            "No explicit deprecated/TODO/legacy/fallback/workaround markers were found "
            "in this module during static review."
        )
        return "\n".join(lines) + "\n"
    for marker in markers:
        text = marker.text.replace("`", "'")
        lines.append(f"- Line {marker.line_number}: `{text}`")
    return "\n".join(lines) + "\n"


def render_readme_section(module_markers: dict[Path, list[MaintenanceMarker]]) -> str:
    lines = [MAINTENANCE_HEADER]
    with_markers = [(path, markers) for path, markers in module_markers.items() if markers]
    if not with_markers:
        lines.append(
            "No explicit deprecated/TODO/legacy/fallback/workaround markers were found "
            "in direct modules during static review."
        )
        return "\n".join(lines) + "\n"
    lines.append("Detailed markers live in the per-module docs; direct module counts:")
    for path, markers in sorted(with_markers):
        lines.append(f"- `{path.name}`: {len(markers)} marker(s)")
    return "\n".join(lines) + "\n"


def replace_maintenance_section(text: str, section: str) -> str:
    pattern = re.compile(
        r"(^## Maintenance Notes\n)(.*?)(?=^## |\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match:
        replacement = section.rstrip() + ("\n" if match.end() == len(text) else "\n\n")
        return pattern.sub(replacement, text, count=1)
    suffix = "" if text.endswith("\n") else "\n"
    return f"{text}{suffix}\n{section.rstrip()}\n"


def planned_updates(root: Path) -> dict[Path, str]:
    scanned = scan_tree(root)
    updates: dict[Path, str] = {}
    readme_modules: dict[Path, dict[Path, list[MaintenanceMarker]]] = {}
    for source, markers in scanned.items():
        doc = doc_path_for_source(root, source)
        if doc.exists():
            current = doc.read_text(encoding="utf-8")
            updates[doc] = replace_maintenance_section(
                current,
                render_module_section(markers),
            )
        readme = readme_path_for_source(root, source)
        readme_modules.setdefault(readme, {})[source] = markers
    for readme, module_markers in readme_modules.items():
        if not readme.exists():
            continue
        current = readme.read_text(encoding="utf-8")
        updates[readme] = replace_maintenance_section(
            current,
            render_readme_section(module_markers),
        )
    return updates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("src/ae"))
    parser.add_argument("--write", action="store_true", help="Rewrite docs in place")
    parser.add_argument("--check", action="store_true", help="Fail if docs are stale")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    updates = planned_updates(root)
    changed: list[Path] = []
    for path, new_text in updates.items():
        old_text = path.read_text(encoding="utf-8")
        if old_text == new_text:
            continue
        changed.append(path)
        if args.write:
            path.write_text(new_text, encoding="utf-8")
        elif args.check:
            rel = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path
            diff = "\n".join(
                difflib.unified_diff(
                    old_text.splitlines(),
                    new_text.splitlines(),
                    fromfile=str(rel),
                    tofile=f"{rel} (expected)",
                    lineterm="",
                )
            )
            print(diff, file=sys.stderr)
    if args.check and changed:
        print(f"maintenance notes stale: {len(changed)} file(s)", file=sys.stderr)
        return 1
    if not args.check:
        action = "updated" if args.write else "would update"
        print(f"{action} {len(changed)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
