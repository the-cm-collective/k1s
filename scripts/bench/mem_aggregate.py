#!/usr/bin/env python3
"""
Aggregate memory snapshots produced by scripts/bench/mem_snapshot.sh.

Inputs: snapshots/<label>/<timestamp>/raw/*
Outputs: summary.json, summary.csv with totals and breakdowns.

This focuses on robust, best-effort parsing and avoids strict dependencies.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class ProcRollup:
    pid: int
    comm: str
    rss_kb: int | None = None
    pss_kb: int | None = None
    uss_kb: int | None = None  # approx from Private_* if available


def _read_smaps_rollup(path: Path) -> Tuple[int | None, int | None, int | None]:
    rss = pss = uss = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("Rss:"):
                    rss = int(line.split()[1])  # kB
                elif line.startswith("Pss:"):
                    pss = int(line.split()[1])
                elif line.startswith("Private_Dirty:") or line.startswith("Private_Clean:"):
                    # we approximate USS by summing private
                    uss = (uss or 0) + int(line.split()[1])
    except Exception:
        pass
    return rss, pss, uss


def _parse_ps(ps_path: Path) -> Dict[int, str]:
    out: Dict[int, str] = {}
    try:
        with ps_path.open("r", encoding="utf-8", errors="ignore") as fh:
            next(fh, None)  # header
            for line in fh:
                parts = line.strip().split(None, 3)
                if len(parts) >= 3:
                    pid = int(parts[0])
                    comm = parts[2]
                    out[pid] = comm
    except Exception:
        pass
    return out


def _collect_proc_rollups(raw_dir: Path) -> List[ProcRollup]:
    ps_map = _parse_ps(raw_dir / "ps_after.txt") or _parse_ps(raw_dir / "ps_before.txt")
    rollups: List[ProcRollup] = []
    for sm in raw_dir.glob("smaps_*_*.txt"):
        # smaps_<pid>_<comm>.txt
        try:
            pid = int(sm.name.split("_")[1])
        except Exception:
            continue
        comm = ps_map.get(pid, sm.name.split("_", 2)[-1].rsplit(".", 1)[0])
        rss, pss, uss = _read_smaps_rollup(sm)
        rollups.append(ProcRollup(pid=pid, comm=comm, rss_kb=rss, pss_kb=pss, uss_kb=uss))
    return rollups


def _read_containers_csv(raw_dir: Path) -> List[Dict[str, str]]:
    path = raw_dir / "containers_mem.csv"
    if not path.exists():
        return []
    out: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        cr = csv.DictReader(fh)
        for row in cr:
            out.append(row)
    return out


def _classify_proc(comm: str, mode: str) -> str:
    comm_l = comm.lower()
    if "containerd-shim" in comm_l:
        return "ignore"
    if mode == "k3s":
        if "k3s" in comm_l:
            return "control_plane"
        if "containerd" in comm_l:
            return "containerd"
        if "coredns" in comm_l:
            return "dns"
        if "traefik" in comm_l:
            return "ingress"
        return "other"
    # k1s default
    if "ae.controller" in comm_l:
        return "controller"
    if "caddy" in comm_l:
        return "ingress"
    if "dockerd" in comm_l or "containerd" in comm_l:
        return "runtime"
    return "other"


def aggregate(snapshot_dir: Path) -> Dict:
    meta = {}
    try:
        meta = json.loads((snapshot_dir / "meta.json").read_text())
    except Exception:
        meta = {}
    mode = str(meta.get("mode", "k1s"))
    raw = snapshot_dir / "raw"

    procs = _collect_proc_rollups(raw)
    proc_totals = {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0}
    by_class: Dict[str, Dict[str, int]] = {}
    for pr in procs:
        c = _classify_proc(pr.comm or "", mode)
        if c == "ignore":
            continue
        bucket = by_class.setdefault(c, {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0})
        if pr.rss_kb is not None:
            proc_totals["rss_kb"] += pr.rss_kb
            bucket["rss_kb"] += pr.rss_kb
        if pr.pss_kb is not None:
            proc_totals["pss_kb"] += pr.pss_kb
            bucket["pss_kb"] += pr.pss_kb
        if pr.uss_kb is not None:
            proc_totals["uss_kb"] += pr.uss_kb
            bucket["uss_kb"] += pr.uss_kb

    # Containers
    containers = _read_containers_csv(raw)
    app_bytes = system_bytes = 0
    if containers:
        # Attempt to classify app vs system using docker inspect labels (if available)
        # Fallback: name prefix heuristics
        inspect = None
        insp_path = raw / "docker_inspect.json"
        if insp_path.exists():
            try:
                inspect = {c.get("Id", "")[:12]: c for c in json.loads(insp_path.read_text())}
            except Exception:
                inspect = None
        for row in containers:
            cid = (row.get("container_id") or "")
            mem = int(row.get("mem_current_bytes") or "-1")
            if mem < 0:
                continue
            is_app = False
            if inspect is not None and cid in inspect:
                labels = (inspect[cid].get("Config") or {}).get("Labels") or {}
                if any(k.startswith("ae.app") for k in labels.keys()) or labels.get("ae.app"):
                    is_app = True
            else:
                name = (row.get("name") or "").lower()
                if name.startswith("ae-") or "rev" in name:
                    is_app = True
            if is_app:
                app_bytes += mem
            else:
                system_bytes += mem

    # Overhead estimate (favor container cgroup sums when available)
    # Report both process PSS and container cgroup bytes
    summary = {
        "meta": meta,
        "process_totals_kb": proc_totals,
        "process_breakdown_kb": by_class,
        "containers": {
            "app_mem_bytes": app_bytes,
            "system_mem_bytes": system_bytes,
            "total_mem_bytes": app_bytes + system_bytes,
        },
        "overhead": {
            "pss_kb_control_plane": int(sum(v.get("pss_kb", 0) for k, v in by_class.items() if k != "other")),
            "cgroup_system_overhead_bytes": int(system_bytes),
        },
    }
    # Per-pod overhead left for higher-level aggregator where replica count is known
    return summary


def write_outputs(snapshot_dir: Path, summary: Dict) -> None:
    (snapshot_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    # Also emit a tiny CSV with key totals
    csv_path = snapshot_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        cw = csv.writer(fh)
        cw.writerow(["label", "mode", "pss_kb_total", "pss_kb_control_plane", "app_mem_bytes", "system_mem_bytes"]) 
        meta = summary.get("meta", {})
        cw.writerow([
            meta.get("label", ""),
            meta.get("mode", ""),
            summary.get("process_totals_kb", {}).get("pss_kb", 0),
            summary.get("overhead", {}).get("pss_kb_control_plane", 0),
            summary.get("containers", {}).get("app_mem_bytes", 0),
            summary.get("containers", {}).get("system_mem_bytes", 0),
        ])


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: scripts/bench/mem_aggregate.py <snapshot_dir>")
        return 2
    snap = Path(argv[1]).resolve()
    if not snap.exists():
        print(f"snapshot not found: {snap}")
        return 2
    summary = aggregate(snap)
    write_outputs(snap, summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
