#!/usr/bin/env python3
"""
Combine multiple snapshot summaries into a single CSV/JSON for charting.

Usage:
  scripts/bench/mem_combine.py snapshots/<label_pattern>

Examples:
  scripts/bench/mem_combine.py snapshots/baseline-*
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from pathlib import Path
from typing import List, Dict


def load_summary(dirpath: Path) -> Dict:
    try:
        return json.loads((dirpath / "summary.json").read_text())
    except Exception:
        return {}


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: scripts/bench/mem_combine.py <snapshots/glob>")
        return 2
    paths: List[Path] = []
    for arg in argv[1:]:
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
                "control_plane_pss_kb": s.get("overhead", {}).get("pss_kb_control_plane", 0),
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
                "mem_available_delta_bytes": (s.get("mem_available", {}) or {}).get(
                    "delta_bytes", 0
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

    # Write combined outputs next to the first directory's parent
    outdir = Path("combined")
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
