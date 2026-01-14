#!/usr/bin/env python3
# ruff: noqa
from __future__ import annotations

from pathlib import Path
import subprocess


def main() -> int:
    missing = []
    snaps = Path("snapshots")
    for lbl in sorted(snaps.glob("*")):
        if not lbl.is_dir():
            continue
        for ts in sorted(lbl.glob("*")):
            if not ts.is_dir():
                continue
            if (ts / "meta.json").exists() and not (ts / "summary.json").exists():
                missing.append(ts)
    print(f"[backfill] found {len(missing)} snapshots to aggregate")
    for p in missing:
        print(f"[backfill] aggregating {p}")
        subprocess.run(["python", "scripts/bench/mem_aggregate.py", str(p)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
