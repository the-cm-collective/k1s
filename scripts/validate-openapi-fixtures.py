#!/usr/bin/env python3
"""
Validate committed OpenAPI schemas against sample manifests.

This is a lightweight stand‑in for kubectl/helm dry-run checks when a live
cluster is unavailable. It loads `docs/openapi/openapi-v2.json`, builds a map
from group/version/kind to the corresponding definition, and runs jsonschema
validation against manifests in specs/examples/ and samples/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple

import yaml
from jsonschema import Draft4Validator, RefResolver, ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "docs/openapi/openapi-v2.json"
MANIFEST_DIRS = [
    REPO_ROOT / "specs" / "examples",
    REPO_ROOT / "samples",
]


def load_openapi() -> dict:
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_gvk_map(openapi: dict) -> Dict[Tuple[str, str, str], str]:
    """Derive (group, version, kind) from definition names.

    The committed spec omits x-kubernetes-group-version-kind so we parse the
    definition key: io.k8s.api.<group>.<version>.<Kind>. Only keep entries that
    expose apiVersion/kind properties (top-level resources).
    """

    mapping: Dict[Tuple[str, str, str], str] = {}
    prefix = "io.k8s.api."

    group_overrides = {
        "networking": "networking.k8s.io",
        "authentication": "authentication.k8s.io",
        "authorization": "authorization.k8s.io",
        "admissionregistration": "admissionregistration.k8s.io",
        "apiregistration": "apiregistration.k8s.io",
        "rbac": "rbac.authorization.k8s.io",
        "storage": "storage.k8s.io",
        "scheduling": "scheduling.k8s.io",
        "node": "node.k8s.io",
        "flowcontrol": "flowcontrol.apiserver.k8s.io",
    }

    for name, schema in openapi.get("definitions", {}).items():
        if not name.startswith(prefix):
            continue
        if "apiVersion" not in schema.get("properties", {}) or "kind" not in schema.get("properties", {}):
            continue

        tail = name[len(prefix) :].split(".")
        if len(tail) < 3:
            continue

        kind = tail[-1]
        version = tail[-2]
        group = ".".join(tail[:-2])
        if group == "core":
            group = ""
        group = group_overrides.get(group, group)

        mapping[(group, version, kind)] = name

    return mapping


def iter_manifests(paths: Iterable[Path]):
    for path in paths:
        if path.is_dir():
            for child in sorted(path.glob("*.yaml")):
                yield from iter_manifests([child])
        elif path.suffix in {".yml", ".yaml"}:
            docs = list(yaml.safe_load_all(path.read_text()))
            for idx, doc in enumerate(docs, start=1):
                if doc:
                    yield path, idx, doc


def validate_docs(gvk_map: Dict[Tuple[str, str, str], str], openapi: dict) -> int:
    resolver = RefResolver.from_schema(openapi)
    errors = 0

    for path, idx, doc in iter_manifests(MANIFEST_DIRS):
        api_version = str(doc.get("apiVersion", ""))
        kind = str(doc.get("kind", ""))

        if "/" in api_version:
            group, version = api_version.split("/", 1)
        else:
            group, version = "", api_version

        ref = gvk_map.get((group, version, kind))
        if not ref:
            print(f"[SKIP] {path.name}#{idx}: no schema for {api_version} {kind}")
            continue

        schema = {"$ref": f"#/definitions/{ref}", **{k: v for k, v in openapi.items() if k == "definitions"}}
        validator = Draft4Validator(schema, resolver=resolver)
        try:
            validator.validate(doc)
            print(f"[OK]    {path.name}#{idx}: {api_version} {kind}")
        except ValidationError as exc:
            errors += 1
            print(f"[FAIL]  {path.name}#{idx}: {api_version} {kind}")
            print(f"        {exc.message}")
    return errors


def main() -> int:
    openapi = load_openapi()
    gvk_map = build_gvk_map(openapi)
    errors = validate_docs(gvk_map, openapi)

    if errors:
        print(f"\nValidation failed for {errors} manifest(s).")
        return 1

    print("\nAll manifests validated against OpenAPI v2 definitions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
