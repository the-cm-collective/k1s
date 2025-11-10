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
import stat
from typing import Dict, List, Tuple


@dataclass
class ProcRollup:
    pid: int
    comm: str
    cmdline: str | None = None  # full args from ps_scan_before.txt when available
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


def _parse_ps_scan(ps_scan_path: Path) -> Dict[int, str]:
    """Parse pid -> full command line from ps_scan_before.txt.

    The file is produced by `ps -eo pid,ppid,comm,args --sort -rss`.
    We want the args column (4th), which may contain spaces; capture the
    substring after the first three whitespace‑separated fields.
    """
    out: Dict[int, str] = {}
    try:
        with ps_scan_path.open("r", encoding="utf-8", errors="ignore") as fh:
            next(fh, None)  # header
            for line in fh:
                # Split into at most 4 parts so the 4th is the full args
                parts = line.rstrip("\n").split(None, 3)
                if len(parts) == 4:
                    pid = int(parts[0])
                    args = parts[3]
                    out[pid] = args
    except Exception:
        pass
    return out


def _collect_proc_rollups(raw_dir: Path) -> List[ProcRollup]:
    ps_map = _parse_ps(raw_dir / "ps_after.txt") or _parse_ps(raw_dir / "ps_before.txt")
    ps_args = _parse_ps_scan(raw_dir / "ps_scan_before.txt")
    rollups: List[ProcRollup] = []
    for sm in raw_dir.glob("smaps_*_*.txt"):
        # smaps_<pid>_<comm>.txt
        try:
            pid = int(sm.name.split("_")[1])
        except Exception:
            continue
        comm = ps_map.get(pid, sm.name.split("_", 2)[-1].rsplit(".", 1)[0])
        cmdline = ps_args.get(pid)
        rss, pss, uss = _read_smaps_rollup(sm)
        rollups.append(
            ProcRollup(pid=pid, comm=comm, cmdline=cmdline, rss_kb=rss, pss_kb=pss, uss_kb=uss)
        )
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


def _classify_proc(comm: str, mode: str, cmdline: str | None = None) -> str:
    comm_l = (comm or "").lower()
    args_l = (cmdline or "").lower()
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
    # k1s default — match either the short comm or full args
    if "ae.controller" in comm_l or "ae.controller" in args_l:
        return "controller"
    if "caddy" in comm_l or "caddy" in args_l:
        return "ingress"
    # Container runtime: docker/containerd/podman/conmon (rootless podman)
    if (
        "dockerd" in comm_l
        or "containerd" in comm_l
        or "podman" in comm_l
        or "conmon" in comm_l
        or "dockerd" in args_l
        or "containerd" in args_l
        or "podman" in args_l
        or "conmon" in args_l
    ):
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
        c = _classify_proc(pr.comm or "", mode, pr.cmdline)
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

    # --- Host system cgroups (cg2 preferred) ---------------------------------
    def _detect_cgv2() -> bool:
        try:
            return Path("/sys/fs/cgroup/cgroup.controllers").exists()
        except Exception:
            return False

    def _dir_is_leaf(p: Path) -> bool:
        """Return True if p has no child directories that are cgroups.

        Heuristic: any immediate child directory containing a file named
        'cgroup.procs' indicates a nested cgroup. We also treat directories
        without subdirectories as leaves.
        """
        try:
            for ch in p.iterdir():
                if not ch.is_dir():
                    continue
                if (ch / "cgroup.procs").exists() or (ch / "cgroup.events").exists():
                    return False
        except Exception:
            return True
        return True

    def _safe_read_int(path: Path) -> int:
        try:
            return int(path.read_text().strip())
        except Exception:
            return 0

    def _sum_leaf_memory_current(root: Path) -> int:
        total = 0
        try:
            if not root.exists():
                return 0
            # Walk recursively. Sum memory.current for leaves only.
            for p in root.rglob("*"):
                try:
                    if not p.is_dir():
                        continue
                except Exception:
                    continue
                mc = p / "memory.current"
                if mc.exists() and _dir_is_leaf(p):
                    total += _safe_read_int(mc)
        except Exception:
            return 0
        return total

    def _sum_host_system_cgroups_bytes() -> int:
        # Exclude user.slice entirely. Include system.slice and init.scope.
        if _detect_cgv2():
            base = Path("/sys/fs/cgroup")
            system_slice = base / "system.slice"
            init_scope = base / "init.scope"
            return _sum_leaf_memory_current(system_slice) + _safe_read_int(
                init_scope / "memory.current"
            )
        else:
            # cgroup v1 fallback: system.slice under memory hierarchy
            base = Path("/sys/fs/cgroup/memory")
            system_slice = base / "system.slice"
            init_scope = base / "init.scope"
            total = 0
            # Best-effort: sum *.scope/*.service usage_in_bytes as leaves
            try:
                for p in system_slice.rglob("*.scope"):
                    u = p / "memory.usage_in_bytes"
                    if u.exists():
                        total += _safe_read_int(u)
                for p in system_slice.rglob("*.service"):
                    u = p / "memory.usage_in_bytes"
                    if u.exists():
                        total += _safe_read_int(u)
                # init.scope single file
                u = init_scope / "memory.usage_in_bytes"
                if u.exists():
                    total += _safe_read_int(u)
            except Exception:
                pass
            return total

    # Parse MemAvailable (bytes) from free -b output before/after snapshot
    def _read_free_available(path: Path) -> int:
        try:
            txt = path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if not txt:
                return 0
            # Find header line containing 'available'
            header_idx = None
            for i, ln in enumerate(txt):
                if ln.lower().startswith("              total") or (
                    "available" in ln.lower() and "mem:" not in ln.lower()
                ):
                    header_idx = i
                    break
            # Fallback: assume first line is header if not found
            if header_idx is None:
                header_idx = 0
            headers = txt[header_idx].lower().split()
            avail_col = None
            for i, h in enumerate(headers):
                if h.startswith("available") or h == "avail":
                    avail_col = i
                    break
            # Find the Mem: row
            mem_line = None
            for ln in txt[header_idx + 1 :]:
                if ln.lower().startswith("mem:"):
                    mem_line = ln
                    break
            if mem_line is None or avail_col is None:
                return 0
            parts = mem_line.split()
            # mem_line typically like: "Mem:  total used free shared buff/cache available"
            # After splitting, last column should be available bytes
            try:
                # Try using header index first
                val = int(parts[avail_col + 1])  # +1 offset for 'Mem:' token
            except Exception:
                val = int(parts[-1])
            return val
        except Exception:
            return 0

    # Containers
    containers = _read_containers_csv(raw)
    app_bytes = system_bytes = 0
    if containers:
        # Attempt to classify app vs system using container inspect labels
        # Merge inspect from both docker and podman when available
        inspect: Dict[str, Dict] = {}
        try:
            insp_d = raw / "docker_inspect.json"
            if insp_d.exists():
                for c in json.loads(insp_d.read_text()):
                    inspect[c.get("Id", "")[:12]] = c
        except Exception:
            pass
        try:
            insp_p = raw / "podman_inspect.json"
            if insp_p.exists():
                for c in json.loads(insp_p.read_text()):
                    inspect[c.get("Id", "")[:12]] = c
        except Exception:
            pass
        for row in containers:
            cid = row.get("container_id") or ""
            mem = int(row.get("mem_current_bytes") or "-1")
            if mem < 0:
                continue
            is_app = False
            if cid in inspect:
                # Docker and Podman both expose Config.Labels; also check top-level Labels for safety
                ins = inspect[cid]
                labels = ((ins.get("Config") or {}).get("Labels") or {}) or (
                    ins.get("Labels") or {}
                )
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

    host_system_bytes = _sum_host_system_cgroups_bytes()
    mem_avail_before = _read_free_available(raw / "free_before.txt")
    mem_avail_after = _read_free_available(raw / "free_after.txt")
    mem_avail_delta = (
        max(0, mem_avail_before - mem_avail_after) if (mem_avail_before and mem_avail_after) else 0
    )

    # Overhead estimate (favor container cgroup sums when available)
    # Report both process PSS and container cgroup bytes
    # Summarize breakdown buckets in MiB for quick human readout
    def kb_to_mib(x: int | None) -> float:
        try:
            return round(float(x or 0) / 1024.0, 2)
        except Exception:
            return 0.0

    ctrl_mib = kb_to_mib((by_class.get("controller") or {}).get("pss_kb", 0))
    ingress_mib = kb_to_mib((by_class.get("ingress") or {}).get("pss_kb", 0))
    runtime_mib = kb_to_mib(((by_class.get("runtime") or {}).get("pss_kb", 0)))
    k3s_cp_mib = kb_to_mib((by_class.get("control_plane") or {}).get("pss_kb", 0))

    summary = {
        "meta": meta,
        "process_totals_kb": proc_totals,
        "process_breakdown_kb": by_class,
        "pss_breakdown_mib": {
            "controller": ctrl_mib,
            "ingress": ingress_mib,
            "runtime": runtime_mib,
            "k3s_control_plane": k3s_cp_mib,
        },
        "containers": {
            "app_mem_bytes": app_bytes,
            "system_mem_bytes": system_bytes,
            "total_mem_bytes": app_bytes + system_bytes,
        },
        "overhead": {
            "pss_kb_control_plane": int(
                sum(v.get("pss_kb", 0) for k, v in by_class.items() if k != "other")
            ),
            # Non-app containers (runtime/infra)
            "cgroup_system_overhead_bytes": int(system_bytes),
            # Host services only (system.slice + init.scope, leaf-summed)
            "host_system_cgroups_bytes": int(host_system_bytes),
        },
        "mem_available": {
            "before_bytes": int(mem_avail_before),
            "after_bytes": int(mem_avail_after),
            "delta_bytes": int(mem_avail_delta),
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
        cw.writerow(
            [
                "label",
                "mode",
                "pss_kb_total",
                "pss_kb_control_plane",
                "app_mem_bytes",
                "system_mem_bytes",
            ]
        )
        meta = summary.get("meta", {})
        cw.writerow(
            [
                meta.get("label", ""),
                meta.get("mode", ""),
                summary.get("process_totals_kb", {}).get("pss_kb", 0),
                summary.get("overhead", {}).get("pss_kb_control_plane", 0),
                summary.get("containers", {}).get("app_mem_bytes", 0),
                summary.get("containers", {}).get("system_mem_bytes", 0),
            ]
        )


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
