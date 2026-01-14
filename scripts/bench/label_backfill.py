#!/usr/bin/env python3
# ruff: noqa
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _detect_oci_from_snapshot(snap: Path) -> str:
    """Best-effort OCI runtime detection from snapshot raw inspect files.

    Prefer Podman inspect ('.OCIRuntime'), then Docker inspect (HostConfig.Runtime/Runtime).
    Returns an empty string when not found.
    """
    try:
        raw = snap / "raw"
        pj = raw / "podman_inspect.json"
        if pj.exists():
            data = json.loads(pj.read_text() or "[]")
            if isinstance(data, list) and data:
                oci = (data[0].get("OCIRuntime") or "").strip()
                if oci:
                    return oci
        dj = raw / "docker_inspect.json"
        if dj.exists():
            data = json.loads(dj.read_text() or "[]")
            if isinstance(data, list) and data:
                obj = data[0] or {}
                oci = (
                    (obj.get("HostConfig") or {}).get("Runtime")
                    or obj.get("Runtime")
                    or ""
                )
                if isinstance(oci, str) and oci.strip():
                    return oci.strip()
    except Exception:
        pass
    return ""


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


def _insert_oci_into_label(label: str, *, backend: str, oci: str) -> str:
    """Insert '+<oci>+' into human label strings.

    Primary path: if '+<backend>+' is present, insert right after it.
    Fallback path: for labels without backend tokens (e.g., 'baseline-...'),
    insert '+<oci>+' before the stage suffix ('-idle', '-pods-N',
    '-rollout-N-(during|post)'). If no suffix detected, append at the end.
    """
    if not label or not oci:
        return label
    token_b = f"+{backend}+" if backend else None
    token_o = f"+{oci}+"
    if token_b and token_b in label and token_o not in label:
        return label.replace(token_b, f"{token_b}{oci}+")

    # Fallback: inject before stage suffix
    import re

    # Known stage patterns in our labels
    m = (
        re.search(r"(-idle)$", label)
        or re.search(r"(-pods-\d+)$", label)
        or re.search(r"(-rollout-\d+-(during|post))$", label)
    )
    if m and token_o not in label:
        start, end = m.span(1)
        return f"{label[:start]}{token_o}{label[start:]}"

    # As a last resort, append once
    if token_o not in label:
        return label + token_o.rstrip("+")
    return label


def patch_snapshot(snap: Path, oci: str, insert_into_label: bool) -> bool:
    meta_path = snap / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    changed = False
    # If no explicit/host-detected value, try to infer from the snapshot itself
    if not oci:
        oci = _detect_oci_from_snapshot(snap)
    if not meta.get("oci_runtime") and oci:
        meta["oci_runtime"] = oci
        changed = True
    label = meta.get("label") or ""
    backend = (meta.get("backend") or "").lower()
    if insert_into_label and oci:
        new_label = _insert_oci_into_label(label, backend=backend, oci=oci.lower())
        if new_label != label:
            meta["label"] = new_label
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
