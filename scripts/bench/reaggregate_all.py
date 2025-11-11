#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> int:
    root = Path("snapshots")
    snaps: list[Path] = []
    for lbl in sorted(root.glob("*")):
        if not lbl.is_dir():
            continue
        for ts in sorted(lbl.glob("*")):
            if ts.is_dir() and (ts / "meta.json").exists():
                snaps.append(ts)
    print(f"[reaggregate] running mem_aggregate on {len(snaps)} snapshots")
    for s in snaps:
        # Only process if directory is writable to avoid root-owned snapshots failing
        try:
            probe = s / ".__write_probe__"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            print(f"[reaggregate] skip (not writable): {s}")
            continue
        subprocess.run(["python", "scripts/bench/mem_aggregate.py", str(s)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
