#!/usr/bin/env python3
# ruff: noqa
"""
Combine multiple snapshot summaries into a single CSV/JSON for charting.

Usage:
  scripts/bench/mem_combine.py [--outdir DIR] snapshots/<label_pattern>

Examples:
  scripts/bench/mem_combine.py snapshots/baseline-*
  scripts/bench/mem_combine.py --outdir state/bench-experiments/demo/combined snapshots/demo-*
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from pathlib import Path
from typing import List, Dict


def _parse_args(argv: List[str]) -> tuple[Path, list[str]]:
    args = list(argv[1:])
    outdir = Path("combined")
    globs: list[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--outdir":
            if idx + 1 >= len(args):
                raise ValueError("--outdir requires a path")
            outdir = Path(args[idx + 1])
            idx += 2
            continue
        globs.append(arg)
        idx += 1
    if not globs:
        raise ValueError("usage: scripts/bench/mem_combine.py [--outdir DIR] <snapshots/glob>")
    return outdir, globs


def load_summary(dirpath: Path) -> Dict:
    try:
        return json.loads((dirpath / "summary.json").read_text())
    except Exception:
        return {}


def main(argv: List[str]) -> int:
    try:
        outdir, input_globs = _parse_args(argv)
    except ValueError as exc:
        print(str(exc))
        return 2
    paths: List[Path] = []
    for arg in input_globs:
        for p in glob.glob(arg):
            # Accept either snapshot directories or parent label directories
            pth = Path(p)
            if (pth / "summary.json").exists():
                paths.append(pth)
            else:
                # Collect child snapshots
                for ch in sorted(pth.glob("*/")):
                    if (ch / "summary.json").exists():
                        paths.append(ch)

    rows: List[Dict] = []
    for snap in sorted(paths):
        s = load_summary(snap)
        if not s:
            continue
        meta = s.get("meta", {})
        rows.append(
            {
                "label": meta.get("label", ""),
                "mode": meta.get("mode", ""),
                "backend": meta.get("backend", ""),
                "oci_runtime": meta.get("oci_runtime", ""),
                "timestamp": meta.get("timestamp", ""),
                "process_pss_kb": s.get("process_totals_kb", {}).get("pss_kb", 0),
                # Historical, misleading name kept for compatibility; charts/docs will prefer new fields below
                "control_plane_pss_kb": s.get("overhead", {}).get("pss_kb_control_plane", 0),
                # New structured fields
                "overhead_pss_kb_total": s.get("overhead", {}).get(
                    "pss_kb_total_overhead", s.get("overhead", {}).get("pss_kb_control_plane", 0)
                ),
                "controller_pss_kb": s.get("overhead", {}).get("pss_kb_controller", 0),
                "ingress_pss_kb": s.get("overhead", {}).get("pss_kb_ingress", 0),
                "runtime_pss_kb": s.get("overhead", {}).get("pss_kb_runtime", 0),
                "k3s_control_plane_pss_kb": s.get("overhead", {}).get(
                    "pss_kb_k3s_control_plane", 0
                ),
                "app_mem_bytes": s.get("containers", {}).get("app_mem_bytes", 0),
                "system_mem_bytes": s.get("containers", {}).get("system_mem_bytes", 0),
                "host_system_cgroups_bytes": s.get("overhead", {}).get(
                    "host_system_cgroups_bytes", 0
                ),
                "mem_available_before_bytes": (s.get("mem_available", {}) or {}).get(
                    "before_bytes", 0
                ),
                "mem_available_after_bytes": (s.get("mem_available", {}) or {}).get(
                    "after_bytes", 0
                ),
                # Backfill mem-available delta if snapshot didn't compute it
                "mem_available_delta_bytes": (
                    (s.get("mem_available", {}) or {}).get("delta_bytes", 0)
                    if (s.get("mem_available", {}) or {}).get("delta_bytes", 0)
                    else (
                        (s.get("mem_available", {}) or {}).get("after_bytes", 0)
                        - (s.get("mem_available", {}) or {}).get("before_bytes", 0)
                    )
                ),
            }
        )

    if not rows:
        print("no summaries found")
        return 1

    # Sort rows chronologically by timestamp (YYYYMMDD-HHMMSS), fallback to original order
    try:
        rows.sort(key=lambda r: (str(r.get("timestamp", ""))))
    except Exception:
        pass

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "combined.json").write_text(json.dumps(rows, indent=2))
    with (outdir / "combined.csv").open("w", newline="", encoding="utf-8") as fh:
        cw = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        cw.writeheader()
        cw.writerows(rows)
    print(f"wrote {outdir}/combined.json and {outdir}/combined.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
