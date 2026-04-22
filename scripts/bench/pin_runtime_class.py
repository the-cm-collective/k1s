#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a bench-local manifest copy with a forced runtimeClassName."
    )
    parser.add_argument("input", type=Path, help="source manifest path")
    parser.add_argument("output", type=Path, help="output manifest path")
    parser.add_argument(
        "--runtime-class",
        default="runc",
        help="runtimeClassName to enforce in deployment/app specs (default: runc)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.input.is_file():
        print(f"input manifest not found: {args.input}", file=sys.stderr)
        return 2

    docs = list(yaml.safe_load_all(args.input.read_text(encoding="utf-8")))
    updated = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = str(doc.get("kind", "")).strip().lower()
        if kind not in {"app", "deployment"}:
            continue
        spec = doc.setdefault("spec", {})
        if not isinstance(spec, dict):
            print(f"manifest has non-mapping spec in {args.input}", file=sys.stderr)
            return 2
        spec["runtimeClassName"] = args.runtime_class
        updated = True

    if not updated:
        print(
            f"manifest does not contain an app/deployment spec to update: {args.input}",
            file=sys.stderr,
        )
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
