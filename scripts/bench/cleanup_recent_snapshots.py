#!/usr/bin/env python3
"""
Clean up recent benchmark snapshots.

Removes invalid snapshots within the last N hours and optionally removes
quick-* labels (used for ad-hoc tests) in the same window.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, "%Y%m%d-%H%M%S")
    except Exception:
        return None


def containers_total(summary_path: Path) -> Optional[int]:
    try:
        data = json.loads(summary_path.read_text())
    except Exception:
        return None
    c = data.get("containers") or {}
    try:
        return int(c.get("total_mem_bytes", 0))
    except Exception:
        return None


def containers_csv_empty(path: Path) -> bool:
    try:
        with path.open() as fh:
            rows = list(csv.DictReader(fh))
        return len(rows) == 0
    except Exception:
        return False


def is_idle_label(label: str) -> bool:
    return "idle" in label.lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up recent benchmark snapshots")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Only consider snapshots newer than this many hours (default: 24)",
    )
    parser.add_argument(
        "--no-quick",
        action="store_true",
        help="Do not remove quick-* labels automatically",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without deleting",
    )
    parser.add_argument(
        "--root",
        default="snapshots",
        help="Snapshots root directory (default: snapshots)",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"no snapshots dir at {root}")
        return 0

    cutoff = datetime.now() - timedelta(hours=args.hours)
    to_delete: list[tuple[Path, str]] = []

    for label_dir in root.iterdir():
        if not label_dir.is_dir():
            continue
        label = label_dir.name
        is_quick = label.startswith("quick-")
        for snap_dir in label_dir.iterdir():
            if not snap_dir.is_dir():
                continue
            ts = parse_ts(snap_dir.name)
            if not ts or ts < cutoff:
                continue
            reason: Optional[str] = None
            if is_quick and not args.no_quick:
                reason = "quick label"
            else:
                summary = snap_dir / "summary.json"
                raw_dir = snap_dir / "raw"
                if not summary.exists():
                    reason = "missing summary.json"
                else:
                    total = containers_total(summary)
                    if total is None:
                        reason = "unreadable summary.json"
                    elif total == 0 and not is_idle_label(label):
                        reason = "containers total_mem_bytes=0"
                if reason is None and raw_dir.exists():
                    try:
                        if not any(raw_dir.iterdir()):
                            reason = "raw dir empty"
                    except Exception:
                        pass
                if reason is None:
                    cm = raw_dir / "containers_mem.csv"
                    if cm.exists() and containers_csv_empty(cm) and not is_idle_label(label):
                        reason = "containers_mem.csv empty"
            if reason:
                to_delete.append((snap_dir, reason))

    if not to_delete:
        print("no snapshots matched for cleanup")
        return 0

    print(f"matched {len(to_delete)} snapshot(s) for cleanup")
    for p, reason in to_delete:
        print(f"- {p} ({reason})")

    if args.dry_run:
        print("dry-run: no deletions performed")
        return 0

    for p, _ in to_delete:
        # rm -rf
        for child in p.rglob("*"):
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
        for child in sorted(p.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        p.rmdir()

    # remove empty label directories
    for label_dir in root.iterdir():
        if label_dir.is_dir():
            try:
                if not any(label_dir.iterdir()):
                    label_dir.rmdir()
            except Exception:
                pass

    print("cleanup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
