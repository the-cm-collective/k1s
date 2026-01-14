#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMBINED = ROOT / "combined" / "combined.csv"


def stat_sig(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
        return int(st.st_mtime), int(st.st_size)
    except FileNotFoundError:
        return (0, 0)


def rebuild_docs() -> None:
    print("[docs-watch] change detected → rebuild docs", flush=True)
    subprocess.run([sys.executable, str(ROOT / "docs" / "build_docs.py")], check=False)


def main() -> int:
    print(f"[docs-watch] watching {COMBINED}")
    last = stat_sig(COMBINED)
    # Build once on start to ensure docs exist
    rebuild_docs()
    try:
        while True:
            cur = stat_sig(COMBINED)
            if cur != last:
                last = cur
                rebuild_docs()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("[docs-watch] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
# ruff: noqa
