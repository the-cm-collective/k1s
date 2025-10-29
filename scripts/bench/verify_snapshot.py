#!/usr/bin/env python3
"""
Verify a memory snapshot by printing per‑container cgroup readings and
the app/system split derived from inspect labels.

Usage:
  python scripts/bench/verify_snapshot.py snapshots/<label>/<timestamp>

Options:
  --json    Output JSON instead of human text.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Row:
    cid: str
    name: str
    pid: str
    bytes: int
    is_app: bool


def _load_json(path: Path) -> Optional[object]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


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


def _load_inspect(raw_dir: Path) -> Dict[str, dict]:
    # Prefer docker inspect; fallback to podman
    insp = raw_dir / "docker_inspect.json"
    if not insp.exists():
        insp = raw_dir / "podman_inspect.json"
    data = _load_json(insp)
    if not isinstance(data, list):
        return {}
    return {str(c.get("Id", ""))[:12]: c for c in data}


def _is_app(cid: str, name: str, inspect: Dict[str, dict]) -> bool:
    if cid in inspect:
        ins = inspect[cid]
        labels = ((ins.get("Config") or {}).get("Labels") or {}) or (ins.get("Labels") or {})
        if any(k.startswith("ae.app") for k in labels.keys()) or labels.get("ae.app"):
            return True
    # Heuristic fallback by name when labels are absent
    nm = (name or "").lower()
    return nm.startswith("ae-") or ("rev" in nm)


def summarize(snapshot_dir: Path) -> dict:
    raw = snapshot_dir / "raw"
    meta = _load_json(snapshot_dir / "meta.json") or {}
    cont_rows = _read_containers_csv(raw)
    inspect = _load_inspect(raw)

    rows: List[Row] = []
    app_b = 0
    sys_b = 0
    for r in cont_rows:
        try:
            cid = (r.get("container_id") or "")
            name = (r.get("name") or "")
            pid = (r.get("pid") or "")
            b = int(r.get("mem_current_bytes") or "-1")
        except Exception:
            continue
        if b < 0:
            continue
        app = _is_app(cid, name, inspect)
        if app:
            app_b += b
        else:
            sys_b += b
        rows.append(Row(cid=cid, name=name, pid=pid, bytes=b, is_app=app))

    # Compare with summary.json if present
    summary_json = _load_json(snapshot_dir / "summary.json") or {}
    agg_app = int(((summary_json.get("containers") or {}).get("app_mem_bytes") or 0))
    agg_sys = int(((summary_json.get("containers") or {}).get("system_mem_bytes") or 0))

    return {
        "meta": meta,
        "rows": [
            {
                "id": r.cid,
                "name": r.name,
                "pid": r.pid,
                "bytes": r.bytes,
                "mib": round(r.bytes / 1048576.0, 3),
                "class": "app" if r.is_app else "system",
            }
            for r in rows
        ],
        "totals": {
            "app_bytes": app_b,
            "system_bytes": sys_b,
            "total_bytes": app_b + sys_b,
            "app_mib": round(app_b / 1048576.0, 3),
            "system_mib": round(sys_b / 1048576.0, 3),
            "total_mib": round((app_b + sys_b) / 1048576.0, 3),
        },
        "summary_json_match": {
            "app_match": (agg_app == app_b),
            "system_match": (agg_sys == sys_b),
            "app_bytes_summary": agg_app,
            "system_bytes_summary": agg_sys,
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python scripts/bench/verify_snapshot.py snapshots/<label>/<timestamp> [--json]", file=sys.stderr)
        return 2
    out_json = False
    args = [a for a in argv[1:] if a != "--json"]
    out_json = (len(argv) > 2 and "--json" in argv[2:])
    snap = Path(args[0]).resolve()
    if not (snap / "raw" / "containers_mem.csv").exists():
        print(f"containers_mem.csv not found under: {snap}", file=sys.stderr)
        return 2
    data = summarize(snap)
    if out_json:
        print(json.dumps(data, indent=2))
        return 0

    meta = data.get("meta", {})
    print(f"Snapshot: label={meta.get('label','')} mode={meta.get('mode','')} ts={meta.get('timestamp','')}")
    print("Containers (by cgroup bytes):")
    print("  id           name                 class    pid      MiB")
    for r in data["rows"]:
        print(f"  {r['id'][:12]:12}  {r['name'][:20]:20}  {r['class'][:6]:6}  {r['pid'][:8]:8}  {r['mib']:7.3f}")
    t = data["totals"]
    print("Totals:")
    print(f"  App cgroups (MiB):    {t['app_mib']}")
    print(f"  System cgroups (MiB): {t['system_mib']}")
    print(f"  Total cgroups (MiB):  {t['total_mib']}")
    m = data["summary_json_match"]
    if (not m["app_match"]) or (not m["system_match"]):
        print("Note: differs from summary.json totals:")
        print(f"  summary app bytes={m['app_bytes_summary']} system bytes={m['system_bytes_summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

