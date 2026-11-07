#!/usr/bin/env python3
"""Resolve and update the single authoritative k1s-core version source."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path


def _version_binding(pyproject: Path) -> tuple[Path, str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    if "version" in project:
        raise ValueError("project.version must be dynamic, not duplicated in pyproject.toml")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or "version" not in dynamic:
        raise ValueError("project.dynamic must declare version")

    setuptools = data.get("tool", {}).get("setuptools", {})
    version_spec = setuptools.get("dynamic", {}).get("version", {})
    attr = version_spec.get("attr") if isinstance(version_spec, dict) else None
    if not isinstance(attr, str) or "." not in attr:
        raise ValueError("tool.setuptools.dynamic.version.attr must name a module attribute")

    module_name, attribute = attr.rsplit(".", 1)
    package_dir = setuptools.get("package-dir", {}).get("", ".")
    if not isinstance(package_dir, str):
        raise ValueError("tool.setuptools.package-dir must map the root package to a path")
    module = pyproject.parent / package_dir / Path(*module_name.split("."))
    source = module / "__init__.py" if module.is_dir() else module.with_suffix(".py")
    return source, attribute


def read_project_version(pyproject: Path) -> str:
    """Read the statically declared authoritative package version."""
    source, attribute = _version_binding(pyproject)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    values: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == attribute for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
    if len(values) != 1:
        raise ValueError(f"{source} must define exactly one literal {attribute}")
    return values[0]


def write_project_version(pyproject: Path, new_version: str) -> Path:
    """Update the authoritative literal while preserving the rest of the source."""
    source, attribute = _version_binding(pyproject)
    text = source.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'^(?P<prefix>{re.escape(attribute)}\s*=\s*)["\'][^"\']+["\']\s*$',
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf'\g<prefix>"{new_version}"', text)
    if count != 1:
        raise ValueError(f"{source} must contain one simple {attribute} assignment")
    source.write_text(updated, encoding="utf-8")
    return source
