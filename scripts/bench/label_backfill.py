#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def detect_backend() -> str:
    # Mirror mem_snapshot.sh behavior
    b = (Path.cwd().joinpath("ENV").read_text() if False else None)  # placeholder
    # Best-effort local detection order
    try:
        subprocess.run(["podman", "--version"], check=False, capture_output=True)
        return "podman"
    except Exception:
        pass
    try:
        subprocess.run(["docker", "--version"], check=False, capture_output=True)
        return "docker"
    except Exception:
        pass
    return "unknown"


def detect_oci(backend: str) -> str:
    try:
        if backend in ("podman", "oci"):
            p = subprocess.run(
                ["podman", "info", "--format", "{{ .Host.OCIRuntime.Name }}"],
                check=False,
                capture_output=True,
                text=True,
            )
            o = (p.stdout or "").strip().strip('"')
            return o
        if backend == "docker":
            p = subprocess.run(
                ["docker", "info", "--format", "{{ .DefaultRuntime }}"],
                check=False,
                capture_output=True,
                text=True,
            )
            return (p.stdout or "").strip().strip('"')
    except Exception:
        pass
    return ""


def patch_snapshot(snap: Path, oci: str, insert_into_label: bool) -> bool:
    meta_path = snap / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    changed = False
    if not meta.get("oci_runtime") and oci:
        meta["oci_runtime"] = oci
        changed = True
    label = meta.get("label") or ""
    backend = (meta.get("backend") or "").lower()
    if insert_into_label and oci and backend:
        token = f"+{oci}+"
        if f"+{backend}+" in label and token not in label:
            label = label.replace(f"+{backend}+", f"+{backend}+{oci}+")
            meta["label"] = label
            changed = True
    if changed:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        # Re-aggregate summary so mem_combine/plots pick up the new meta
        subprocess.run(["python", "scripts/bench/mem_aggregate.py", str(snap)], check=False)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill oci_runtime and label into snapshots")
    ap.add_argument("glob", nargs="+", help="snapshot paths or label dirs (supports globs)")
    ap.add_argument("--oci", default="", help="override oci runtime name (e.g. crun)")
    ap.add_argument(
        "--insert-into-label",
        action="store_true",
        help="also inject '+<oci>+' into meta.label when missing",
    )
    args = ap.parse_args()

    snaps: list[Path] = []
    for g in args.glob:
        for p in Path().glob(g):
            if (p / "summary.json").exists() or (p / "meta.json").exists():
                snaps.append(p)
            else:
                for ch in sorted(p.glob("*/")):
                    if (ch / "meta.json").exists():
                        snaps.append(ch)

    if not snaps:
        print("no snapshots matched")
        return 1

    backend = detect_backend()
    oci = args.oci or detect_oci(backend)
    updated = 0
    for s in snaps:
        if patch_snapshot(s, oci, args.insert_into_label):
            updated += 1
    print(f"patched {updated} snapshots (backend={backend}, oci={oci or 'unknown'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

