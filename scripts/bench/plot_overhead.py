#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path


def main(argv):
    if len(argv) < 2:
        print(
            "usage: scripts/bench/plot_overhead.py combined/combined.csv [outdir]", file=sys.stderr
        )
        return 2
    csv_path = Path(argv[1])
    outdir = Path(argv[2]) if len(argv) > 2 else Path("charts")
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    with csv_path.open("r", encoding="utf-8", errors="ignore") as fh:
        cr = csv.DictReader(fh)
        for r in cr:
            rows.append(r)
    if not rows:
        print("no rows in combined csv", file=sys.stderr)
        return 1

    try:
        import matplotlib.pyplot as plt
    except Exception:
        # Be lenient for CI and environments without plotting; keep pipeline green.
        print("matplotlib not available; install with: pip install matplotlib", file=sys.stderr)
        print("skipping plot generation; combined CSV remains available", file=sys.stderr)
        return 0

    # Figure 1: control-plane PSS by label
    labels = [r["label"] for r in rows]
    pss = [int(r.get("control_plane_pss_kb") or 0) / 1024.0 for r in rows]
    plt.figure(figsize=(10, 4))
    plt.bar(labels, pss, color="#60a5fa")
    plt.ylabel("Control-plane PSS (MiB)")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(outdir / "control_plane_pss.png", dpi=120)
    plt.close()

    # Figure 2: host system cgroup memory by label (system.slice + init.scope)
    sys_mem = [
        int((r.get("host_system_cgroups_bytes") or r.get("system_mem_bytes") or 0))
        / (1024.0 * 1024.0)
        for r in rows
    ]
    plt.figure(figsize=(10, 4))
    plt.bar(labels, sys_mem, color="#34d399")
    plt.ylabel("System cgroups (MiB)")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    plt.savefig(outdir / "system_cgroups.png", dpi=120)
    plt.close()

    # Figure 3: per-pod app footprint ~ app_mem_bytes / N for labels matching *-pods-N
    pods_labels = []
    pods_vals = []
    for r in rows:
        lab = r.get("label", "")
        m = re.search(r"pods-(\d+)", lab)
        if not m:
            continue
        n = int(m.group(1) or 0) or 1
        per_pod = (int(r.get("app_mem_bytes") or 0) / max(1, n)) / (1024.0 * 1024.0)
        pods_labels.append(lab)
        pods_vals.append(per_pod)
    if pods_labels:
        plt.figure(figsize=(10, 4))
        plt.bar(pods_labels, pods_vals, color="#f59e0b")
        plt.ylabel("Per‑pod app mem (MiB) ~ app/replicas")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        plt.savefig(outdir / "per_pod_overhead.png", dpi=120)
        plt.close()

    print(f"wrote charts to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
