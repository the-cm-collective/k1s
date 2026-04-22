#!/usr/bin/env python3
# ruff: noqa
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
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

HOST_SYSTEM_TOP_LIMIT = 10
RUNTIME_PROCESS_TOP_LIMIT = 10
RUNTIME_PROCESS_GROUP_KEYS = (
    "containerd",
    "containerd_shim",
    "conmon",
    "podman",
    "passt",
    "slirp4netns",
    "dockerd",
    "other_runtime",
)


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


def _detect_cgv2() -> bool:
    try:
        return Path("/sys/fs/cgroup/cgroup.controllers").exists()
    except Exception:
        return False


def _dir_is_leaf(path: Path) -> bool:
    try:
        for child in path.iterdir():
            if not child.is_dir():
                continue
            if (child / "cgroup.procs").exists() or (child / "cgroup.events").exists():
                return False
    except Exception:
        return True
    return True


def _safe_read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:
        return 0


def _read_host_system_cgroups_csv(raw_dir: Path) -> List[Dict[str, object]]:
    path = raw_dir / "host_system_cgroups.csv"
    if not path.exists():
        return []
    rows: List[Dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            cr = csv.DictReader(fh)
            for row in cr:
                rows.append(
                    {
                        "path": str(row.get("path") or ""),
                        "bytes": int(row.get("bytes") or 0),
                        "slice_kind": str(row.get("slice_kind") or ""),
                    }
                )
    except Exception:
        return []
    return rows


def _collect_leaf_memory_rows(
    root: Path,
    base: Path,
    *,
    slice_kind: str,
    memory_file: str,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    try:
        if not root.exists():
            return rows
        candidates = [root]
        candidates.extend(sorted(root.rglob("*")))
        for path in candidates:
            try:
                if not path.is_dir():
                    continue
            except Exception:
                continue
            mem_path = path / memory_file
            if mem_path.exists() and _dir_is_leaf(path):
                rows.append(
                    {
                        "path": "/" + str(path.relative_to(base)),
                        "bytes": _safe_read_int(mem_path),
                        "slice_kind": slice_kind,
                    }
                )
    except Exception:
        return []
    return rows


def _collect_live_host_system_cgroups_rows() -> List[Dict[str, object]]:
    if _detect_cgv2():
        base = Path("/sys/fs/cgroup")
        rows = _collect_leaf_memory_rows(
            base / "system.slice",
            base,
            slice_kind="system.slice",
            memory_file="memory.current",
        )
        rows.extend(
            _collect_leaf_memory_rows(
                base / "init.scope",
                base,
                slice_kind="init.scope",
                memory_file="memory.current",
            )
        )
        return rows

    base = Path("/sys/fs/cgroup/memory")
    rows = _collect_leaf_memory_rows(
        base / "system.slice",
        base,
        slice_kind="system.slice",
        memory_file="memory.usage_in_bytes",
    )
    rows.extend(
        _collect_leaf_memory_rows(
            base / "init.scope",
            base,
            slice_kind="init.scope",
            memory_file="memory.usage_in_bytes",
        )
    )
    return rows


def _host_system_cgroups_rows(raw_dir: Path) -> List[Dict[str, object]]:
    rows = _read_host_system_cgroups_csv(raw_dir)
    if rows:
        return rows
    return _collect_live_host_system_cgroups_rows()


def _sum_host_system_cgroups_bytes(rows: List[Dict[str, object]]) -> int:
    total = 0
    for row in rows:
        try:
            total += int(row.get("bytes") or 0)
        except Exception:
            continue
    return total


def _top_host_system_cgroups(
    rows: List[Dict[str, object]], limit: int = HOST_SYSTEM_TOP_LIMIT
) -> List[Dict[str, object]]:
    top_rows = sorted(
        rows,
        key=lambda item: (-int(item.get("bytes") or 0), str(item.get("path") or "")),
    )[:limit]
    summary: List[Dict[str, object]] = []
    for row in top_rows:
        raw_bytes = int(row.get("bytes") or 0)
        summary.append(
            {
                "path": str(row.get("path") or ""),
                "slice_kind": str(row.get("slice_kind") or ""),
                "bytes": raw_bytes,
                "mib": round(raw_bytes / (1024.0 * 1024.0), 2),
            }
        )
    return summary


def _proc_names(comm: str, cmdline: str | None = None) -> tuple[str, ...]:
    names: list[str] = []
    for raw in (comm or "", (cmdline or "").strip().split(None, 1)[0] if cmdline else ""):
        token = raw.strip()
        if not token:
            continue
        token = token.rsplit("/", 1)[-1].lower()
        if token and token not in names:
            names.append(token)
    return tuple(names)


def _runtime_lane_groups(mode: str, meta: Dict[str, object]) -> set[str]:
    mode_l = str(mode or "").lower()
    engine = str(meta.get("engine_filter") or meta.get("backend") or "").lower()
    if engine == "oci":
        engine = "podman"
    if mode_l == "k3s":
        return {"containerd", "dockerd"}
    if engine == "cri":
        return {"containerd", "containerd_shim"}
    if engine == "docker":
        return {"containerd", "dockerd"}
    if engine == "podman":
        return {"podman", "conmon", "passt", "slirp4netns"}
    return {
        "containerd",
        "containerd_shim",
        "conmon",
        "podman",
        "passt",
        "slirp4netns",
        "dockerd",
    }


def _runtime_group_key(comm: str, cmdline: str | None = None) -> str | None:
    for name in _proc_names(comm, cmdline):
        if name.startswith("containerd-shim"):
            return "containerd_shim"
        if name == "containerd":
            return "containerd"
        if name == "conmon":
            return "conmon"
        if name == "podman":
            return "podman"
        if name.startswith("passt") or name == "pasta":
            return "passt"
        if name == "slirp4netns":
            return "slirp4netns"
        if name == "dockerd":
            return "dockerd"
    return None


def _runtime_process_groups(procs: List[ProcRollup], mode: str, meta: Dict[str, object]) -> Dict[str, int]:
    stats = _runtime_process_group_stats(procs, mode, meta)
    return {key: int(group.get("pss_kb", 0) or 0) for key, group in stats.items()}


def _runtime_process_group_stats(
    procs: List[ProcRollup],
    mode: str,
    meta: Dict[str, object],
) -> Dict[str, Dict[str, object]]:
    allowed_groups = _runtime_lane_groups(mode, meta)
    totals: Dict[str, Dict[str, object]] = {
        key: {"count": 0, "pss_kb": 0} for key in RUNTIME_PROCESS_GROUP_KEYS
    }
    for pr in procs:
        group = _runtime_group_key(pr.comm or "", pr.cmdline)
        if group is None:
            if _classify_proc(pr.comm or "", mode, meta, pr.cmdline) == "runtime":
                group = "other_runtime"
            else:
                continue
        elif group not in allowed_groups:
            continue
        totals[group]["count"] = int(totals[group]["count"]) + 1
        totals[group]["pss_kb"] = int(totals[group]["pss_kb"]) + int(pr.pss_kb or 0)
    stats: Dict[str, Dict[str, object]] = {}
    for key, group in totals.items():
        count = int(group["count"] or 0)
        pss_kb = int(group["pss_kb"] or 0)
        mean_pss_kb = int(round(pss_kb / count)) if count > 0 else 0
        stats[key] = {
            "count": count,
            "pss_kb": pss_kb,
            "pss_mib": round(pss_kb / 1024.0, 2),
            "mean_pss_kb": mean_pss_kb,
            "mean_pss_mib": round(mean_pss_kb / 1024.0, 2),
        }
    return stats


def _runtime_process_top(
    procs: List[ProcRollup],
    mode: str,
    meta: Dict[str, object],
    limit: int = RUNTIME_PROCESS_TOP_LIMIT,
) -> List[Dict[str, object]]:
    allowed_groups = _runtime_lane_groups(mode, meta)
    runtime_rows: List[Dict[str, object]] = []
    for pr in procs:
        group = _runtime_group_key(pr.comm or "", pr.cmdline)
        if group is None:
            if _classify_proc(pr.comm or "", mode, meta, pr.cmdline) == "runtime":
                group = "other_runtime"
            else:
                continue
        elif group not in allowed_groups:
            continue
        pss_kb = int(pr.pss_kb or 0)
        runtime_rows.append(
            {
                "pid": int(pr.pid),
                "comm": str(pr.comm or ""),
                "cmdline": str(pr.cmdline or ""),
                "group": group,
                "pss_kb": pss_kb,
                "pss_mib": round(pss_kb / 1024.0, 2),
            }
        )

    runtime_rows.sort(
        key=lambda item: (
            -int(item.get("pss_kb") or 0),
            str(item.get("comm") or ""),
            int(item.get("pid") or 0),
        )
    )
    return runtime_rows[:limit]


def _classify_proc(
    comm: str,
    mode: str,
    meta: Dict[str, object],
    cmdline: str | None = None,
) -> str:
    comm_l = (comm or "").lower()
    args_l = (cmdline or "").lower()
    proc_names = _proc_names(comm, cmdline)
    if any(name.startswith("containerd-shim") for name in proc_names):
        return "ignore"
    if mode == "k3s":
        if any(name == "k3s" for name in proc_names):
            return "control_plane"
        if any(name == "containerd" for name in proc_names):
            return "containerd"
        if any(name == "coredns" for name in proc_names):
            return "dns"
        if any(name == "traefik" for name in proc_names):
            return "ingress"
        return "other"
    # k1s default — match either the short comm or full args
    if "ae.controller" in comm_l or "ae.controller" in args_l:
        return "controller"
    if any(name == "caddy" for name in proc_names) or " caddy" in args_l:
        return "ingress"
    runtime_group = _runtime_group_key(comm, cmdline)
    if runtime_group is not None:
        if runtime_group in _runtime_lane_groups(mode, meta):
            return "runtime"
        return "ignore"
    if "netavark" in proc_names or "aardvark-dns" in proc_names:
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
        c = _classify_proc(pr.comm or "", mode, meta, pr.cmdline)
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

    # k3s extras: allow docker-exec based metrics to fill gaps when smaps aren't readable
    try:
        if mode == "k3s":
            cp_extra_path = raw / "k3s_control_plane_pss_kb.txt"
            if cp_extra_path.exists():
                v = int((cp_extra_path.read_text().strip() or "0"))
                if v > 0:
                    cp_bucket = by_class.setdefault(
                        "control_plane", {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0}
                    )
                    if cp_bucket.get("pss_kb", 0) == 0:
                        cp_bucket["pss_kb"] = v
                        proc_totals["pss_kb"] += v
    except Exception:
        pass

    # k1nd extras: allow docker-exec based metrics to fill gaps when smaps aren't readable
    try:
        if mode == "k1s":
            k1nd_path = raw / "k1nd_control_plane_pss_kb.json"
            if k1nd_path.exists():
                data = json.loads(k1nd_path.read_text() or "{}")
                ctrl = int(data.get("controller_pss_kb", 0) or 0)
                apishim = int(data.get("apishim_pss_kb", 0) or 0)
                ingress = int(data.get("ingress_pss_kb", 0) or 0)
                ctrl_total = ctrl + apishim
                if ctrl_total > 0:
                    ctrl_bucket = by_class.setdefault(
                        "controller", {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0}
                    )
                    if ctrl_bucket.get("pss_kb", 0) == 0:
                        ctrl_bucket["pss_kb"] = ctrl_total
                        proc_totals["pss_kb"] += ctrl_total
                if ingress > 0:
                    ing_bucket = by_class.setdefault(
                        "ingress", {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0}
                    )
                    if ing_bucket.get("pss_kb", 0) == 0:
                        ing_bucket["pss_kb"] = ingress
                        proc_totals["pss_kb"] += ingress
    except Exception:
        pass

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
    app_container_count = system_container_count = 0
    # Track seen cgroup leaves to avoid double-counting when multiple containers
    # share the same cgroup (common with rootless engines without full delegation).
    seen_app_cg: set[str] = set()
    seen_sys_cg: set[str] = set()
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
        try:
            insp_c = raw / "cri_inspect.json"
            if insp_c.exists():
                for c in json.loads(insp_c.read_text()):
                    inspect[c.get("Id", "")[:12]] = c
        except Exception:
            pass
        for row in containers:
            cid = row.get("container_id") or ""
            mem = int(row.get("mem_current_bytes") or "-1")
            if mem < 0:
                continue
            is_app = False
            cg_path = (row.get("cg_path") or row.get("cgroup_path") or "").strip()
            # For k3s mode, treat all host-engine containers as system; rely on k3s extras for app
            if mode == "k3s":
                is_app = False
            else:
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
            # Deduplicate by cgroup path when present; fall back to naive sum
            if is_app:
                app_container_count += 1
                if cg_path:
                    if cg_path in seen_app_cg:
                        continue
                    seen_app_cg.add(cg_path)
                app_bytes += mem
            else:
                system_container_count += 1
                if cg_path:
                    if cg_path in seen_sys_cg:
                        continue
                    seen_sys_cg.add(cg_path)
                system_bytes += mem

    # k3s extras: prefer inner kubepods.sum if present and app_bytes not already populated
    try:
        if mode == "k3s" and app_bytes == 0:
            ap_path = raw / "k3s_app_cgroups_bytes.txt"
            if ap_path.exists():
                v = int((ap_path.read_text().strip() or "0"))
                if v > 0:
                    app_bytes = v
    except Exception:
        pass

    host_system_rows = _host_system_cgroups_rows(raw)
    host_system_bytes = _sum_host_system_cgroups_bytes(host_system_rows)
    runtime_process_group_stats = _runtime_process_group_stats(procs, mode, meta)
    runtime_process_groups = {
        key: int(group.get("pss_kb", 0) or 0) for key, group in runtime_process_group_stats.items()
    }
    runtime_process_top = _runtime_process_top(procs, mode, meta)
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
    pss_kb_runtime = int(sum(runtime_process_groups.values()))
    runtime_mib = kb_to_mib(pss_kb_runtime)
    k3s_cp_mib = kb_to_mib((by_class.get("control_plane") or {}).get("pss_kb", 0))

    # Per-bucket PSS in KiB (None -> 0)
    pss_kb_controller = int((by_class.get("controller") or {}).get("pss_kb", 0) or 0)
    pss_kb_ingress = int((by_class.get("ingress") or {}).get("pss_kb", 0) or 0)
    pss_kb_k3s_cp = int((by_class.get("control_plane") or {}).get("pss_kb", 0) or 0)

    # Historical alias summed almost-everything except "other"; keep it but also expose clearer fields
    pss_kb_total_overhead = int(
        sum(v.get("pss_kb", 0) for k, v in by_class.items() if k != "other")
    )

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
            "app_container_count": int(app_container_count),
            "system_container_count": int(system_container_count),
            "total_container_count": int(app_container_count + system_container_count),
        },
        "overhead": {
            # Historical field (misleading name): retained for backward compatibility
            "pss_kb_control_plane": pss_kb_total_overhead,
            # Clearer, structured fields
            "pss_kb_total_overhead": pss_kb_total_overhead,
            "pss_kb_controller": pss_kb_controller,
            "pss_kb_ingress": pss_kb_ingress,
            "pss_kb_runtime": pss_kb_runtime,
            "pss_kb_k3s_control_plane": pss_kb_k3s_cp,
            # Non-app containers (runtime/infra)
            "cgroup_system_overhead_bytes": int(system_bytes),
            # Host services only (system.slice + init.scope, leaf-summed)
            "host_system_cgroups_bytes": int(host_system_bytes),
            "host_system_cgroups_top": _top_host_system_cgroups(host_system_rows),
            # Additive runtime attribution for internal benchmark analysis.
            "runtime_process_groups": runtime_process_groups,
            "runtime_process_group_stats": runtime_process_group_stats,
            "runtime_process_top": runtime_process_top,
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
