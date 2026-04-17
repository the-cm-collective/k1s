#!/usr/bin/env python3
# ruff: noqa
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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
                oci = (obj.get("HostConfig") or {}).get("Runtime") or obj.get("Runtime") or ""
                if isinstance(oci, str) and oci.strip():
                    return oci.strip()
    except Exception:
        pass
    return ""


def detect_backend() -> str:
    # Mirror mem_snapshot.sh behavior
    b = Path.cwd().joinpath("ENV").read_text() if False else None  # placeholder
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


_STAGE_RE = re.compile(r"-(idle|pods-\d+|rollout-\d+-(?:during|post))$")


def _split_label_stage(label: str) -> tuple[str, str]:
    m = _STAGE_RE.search(label or "")
    if not m:
        return label, ""
    return label[: m.start()], m.group(1)


def _snapshot_dir_label(snap: Path) -> str:
    for candidate in (snap.name, snap.parent.name):
        _, stage = _split_label_stage(candidate)
        if stage:
            return candidate
    return ""


def _stamp_prefix(head: str, *markers: str) -> str:
    for marker in markers:
        idx = head.find(marker)
        if idx != -1:
            return head[:idx]
    return head


def _normalize_label(
    label: str,
    *,
    backend: str,
    oci: str,
    mode: str,
    rootless: bool,
    cgroups: str,
) -> str:
    """Rebuild a canonical label from structured metadata and stage suffix."""
    if not label:
        return label
    head, stage = _split_label_stage(label)
    if not stage:
        return label

    oci_part = f"+{oci}" if oci else ""
    if mode == "k3s":
        stamp = _stamp_prefix(head, "+k3d", "+docker")
        return f"{stamp}+k3d{oci_part}-{stage}"
    if backend == "docker":
        stamp = _stamp_prefix(head, "+docker")
        return f"{stamp}+docker{oci_part}+k1nd-{stage}"
    if backend == "cri":
        m = re.match(r"^(?P<prefix>.+\+cri)(?:\+[^+]+)?\+containerd$", head)
        if m:
            return f"{m.group('prefix')}{oci_part}+containerd-{stage}"
        stamp = _stamp_prefix(head, "+cri")
        return f"{stamp}+cri{oci_part}+containerd-{stage}"
    if backend == "podman":
        stamp = _stamp_prefix(head, "+podman")
        root_tag = "rootless" if rootless else "priv"
        cgroups_tag = cgroups or "cg2"
        return f"{stamp}+podman{oci_part}+{root_tag}+{cgroups_tag}-{stage}"
    return label


def _reaggregate_snapshot(snap: Path) -> None:
    proc = subprocess.run(
        ["python", "scripts/bench/mem_aggregate.py", str(snap)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return
    print(f"[label-backfill] mem_aggregate failed for {snap}", file=sys.stderr)
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    if stderr:
        print(stderr, file=sys.stderr)
    elif stdout:
        print(stdout, file=sys.stderr)


def _summary_message(updated: int, *, oci: str, force_oci: bool) -> str:
    details: list[str] = []
    if force_oci and oci:
        details.append(f"forced_oci={oci}")
    elif updated and oci:
        details.append(f"fallback_oci={oci}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"patched {updated} snapshots{suffix}"


def patch_snapshot(snap: Path, fallback_oci: str, *, force_oci: bool, insert_into_label: bool) -> bool:
    meta_path = snap / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return False
    changed = False
    meta_label = str(meta.get("label") or "")
    label = _snapshot_dir_label(snap) or meta_label
    backend = (meta.get("backend") or "").lower()
    mode = str(meta.get("mode") or "").lower()
    cgroups = str(meta.get("cgroups") or "").strip()
    rootless = bool(meta.get("rootless"))
    meta_oci = str(meta.get("oci_runtime") or "").strip()
    snap_oci = _detect_oci_from_snapshot(snap)
    if force_oci:
        effective_oci = fallback_oci
    elif backend == "cri":
        effective_oci = meta_oci or snap_oci
    else:
        effective_oci = meta_oci or snap_oci or fallback_oci
    if effective_oci and meta_oci != effective_oci:
        meta["oci_runtime"] = effective_oci
        changed = True
    if insert_into_label:
        new_label = label
        if effective_oci:
            new_label = _normalize_label(
                label,
                backend=backend,
                oci=effective_oci.lower(),
                mode=mode,
                rootless=rootless,
                cgroups=cgroups,
            )
        if new_label != meta_label:
            meta["label"] = new_label
            changed = True
    if changed:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        # Re-aggregate summary so mem_combine/plots pick up the new meta
        _reaggregate_snapshot(snap)
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
        if patch_snapshot(
            s,
            oci,
            force_oci=bool(args.oci),
            insert_into_label=args.insert_into_label,
        ):
            updated += 1
    print(_summary_message(updated, oci=oci, force_oci=bool(args.oci)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
