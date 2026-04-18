#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_ROOT = ROOT / "snapshots"
DEFAULT_COMBINED_DIR = ROOT / "combined"
DEFAULT_CHARTS_DIR = ROOT / "charts"
DEFAULT_STATE_DIR = ROOT / "state"
DEFAULT_LEGACY_CSV = ROOT / "scripts" / "bench" / "data" / "legacy_20260203_frozen.csv"

FIELDNAMES = [
    "label",
    "mode",
    "backend",
    "oci_runtime",
    "timestamp",
    "process_pss_kb",
    "control_plane_pss_kb",
    "overhead_pss_kb_total",
    "controller_pss_kb",
    "ingress_pss_kb",
    "runtime_pss_kb",
    "k3s_control_plane_pss_kb",
    "app_mem_bytes",
    "system_mem_bytes",
    "host_system_cgroups_bytes",
    "mem_available_before_bytes",
    "mem_available_after_bytes",
    "mem_available_delta_bytes",
]

NUMERIC_FIELDS = {
    "process_pss_kb",
    "control_plane_pss_kb",
    "overhead_pss_kb_total",
    "controller_pss_kb",
    "ingress_pss_kb",
    "runtime_pss_kb",
    "k3s_control_plane_pss_kb",
    "app_mem_bytes",
    "system_mem_bytes",
    "host_system_cgroups_bytes",
    "mem_available_before_bytes",
    "mem_available_after_bytes",
    "mem_available_delta_bytes",
}

INTERIM_20260417_PREFIXES = (
    "r20260417-cri-runc-baseline-clean5-run1+cri+containerd",
    "r20260417-cri-runc-baseline-clean5-run2+cri+containerd",
    "r20260417-cri-runc-baseline-clean5-run3+cri+containerd",
    "r20260417-overlap-smoke-rootless",
    "r20260417-overlap-smoke-rootful",
    "r20260417-overlap-smoke-cri-run1+cri+containerd",
    "r20260417-overlap-smoke-k1nd",
    "r20260417-overlap-smoke-k3d",
)


def final_prefixes(stamp: str) -> tuple[str, ...]:
    return (
        f"{stamp}+podman+rootless+cg2",
        f"{stamp}+podman+priv+cg2",
        f"{stamp}+docker+k1nd",
        f"{stamp}+k3d",
        f"{stamp}+cri-runc-verify-run1+cri+containerd",
        f"{stamp}+cri-runc-verify-run2+cri+containerd",
        f"{stamp}+cri-runc-verify-run3+cri+containerd",
    )


def keep_prefixes(profile: str, stamp: str | None) -> tuple[str, ...]:
    if profile == "interim-20260417":
        return INTERIM_20260417_PREFIXES
    if profile == "final":
        if not stamp:
            raise ValueError("--stamp is required for --profile final")
        return final_prefixes(stamp)
    raise ValueError(f"unsupported profile: {profile}")


def load_summary(dirpath: Path) -> dict[str, Any]:
    try:
        return json.loads((dirpath / "summary.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def summary_to_row(snap: Path, summary: dict[str, Any]) -> dict[str, Any]:
    meta = summary.get("meta", {})
    mem_available = summary.get("mem_available", {}) or {}
    overhead = summary.get("overhead", {}) or {}
    containers = summary.get("containers", {}) or {}
    delta = mem_available.get("delta_bytes", 0)
    if not delta:
        before = int(mem_available.get("before_bytes", 0) or 0)
        after = int(mem_available.get("after_bytes", 0) or 0)
        delta = after - before
    return {
        "label": meta.get("label", snap.parent.name),
        "mode": meta.get("mode", ""),
        "backend": meta.get("backend", ""),
        "oci_runtime": meta.get("oci_runtime", ""),
        "timestamp": meta.get("timestamp", ""),
        "process_pss_kb": summary.get("process_totals_kb", {}).get("pss_kb", 0),
        "control_plane_pss_kb": overhead.get("pss_kb_control_plane", 0),
        "overhead_pss_kb_total": overhead.get(
            "pss_kb_total_overhead", overhead.get("pss_kb_control_plane", 0)
        ),
        "controller_pss_kb": overhead.get("pss_kb_controller", 0),
        "ingress_pss_kb": overhead.get("pss_kb_ingress", 0),
        "runtime_pss_kb": overhead.get("pss_kb_runtime", 0),
        "k3s_control_plane_pss_kb": overhead.get("pss_kb_k3s_control_plane", 0),
        "app_mem_bytes": containers.get("app_mem_bytes", 0),
        "system_mem_bytes": containers.get("system_mem_bytes", 0),
        "host_system_cgroups_bytes": overhead.get("host_system_cgroups_bytes", 0),
        "mem_available_before_bytes": mem_available.get("before_bytes", 0),
        "mem_available_after_bytes": mem_available.get("after_bytes", 0),
        "mem_available_delta_bytes": delta,
    }


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in FIELDNAMES:
        value = row.get(field, 0 if field in NUMERIC_FIELDS else "")
        if field in NUMERIC_FIELDS:
            try:
                normalized[field] = int(float(value or 0))
            except Exception:
                normalized[field] = 0
        else:
            normalized[field] = str(value or "")
    return normalized


def collect_snapshot_rows(label_dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label_dir in sorted(label_dirs):
        for snap in sorted(path for path in label_dir.iterdir() if path.is_dir()):
            if not (snap / "summary.json").exists():
                continue
            summary = load_summary(snap)
            if not summary:
                continue
            rows.append(normalize_row(summary_to_row(snap, summary)))
    return rows


def load_frozen_rows(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDNAMES:
            raise ValueError(
                f"{csv_path} schema mismatch: expected {FIELDNAMES}, got {reader.fieldnames}"
            )
        return [normalize_row(row) for row in reader]


def partition_snapshot_dirs(
    snapshot_root: Path, prefixes: tuple[str, ...]
) -> tuple[list[Path], list[Path], list[str]]:
    label_dirs = sorted(path for path in snapshot_root.iterdir() if path.is_dir())
    kept: list[Path] = []
    dropped: list[Path] = []
    for label_dir in label_dirs:
        if any(label_dir.name.startswith(prefix) for prefix in prefixes):
            kept.append(label_dir)
        else:
            dropped.append(label_dir)
    missing = [prefix for prefix in prefixes if not any(p.name.startswith(prefix) for p in kept)]
    return kept, dropped, missing


def write_inventory(
    state_dir: Path,
    profile: str,
    prefixes: tuple[str, ...],
    kept: list[Path],
    dropped: list[Path],
    missing: list[str],
) -> tuple[Path, Path]:
    state_dir.mkdir(parents=True, exist_ok=True)
    keep_path = state_dir / "bench-retained.keep.txt"
    drop_path = state_dir / "bench-retained.drop.txt"

    keep_lines = [
        f"profile={profile}",
        "requested_prefixes:",
        *[f"  {prefix}" for prefix in prefixes],
        "kept_snapshot_dirs:",
        *[f"  {path.name}" for path in kept],
    ]
    if missing:
        keep_lines.extend(["missing_prefixes:", *[f"  {prefix}" for prefix in missing]])
    keep_path.write_text("\n".join(keep_lines) + "\n", encoding="utf-8")

    drop_lines = [f"profile={profile}", "dropped_snapshot_dirs:", *[f"  {path.name}" for path in dropped]]
    drop_path.write_text("\n".join(drop_lines) + "\n", encoding="utf-8")
    return keep_path, drop_path


def write_combined(rows: list[dict[str, Any]], combined_dir: Path) -> None:
    combined_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: (str(row.get("timestamp", "")), str(row.get("label", ""))))
    (combined_dir / "combined.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (combined_dir / "combined.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def run_optional_step(name: str, cmd: list[str]) -> bool:
    result = subprocess.run(cmd, cwd=ROOT, check=False)
    if result.returncode == 0:
        return True
    print(f"[retained-rebuild] warning: {name} failed with exit={result.returncode}", file=sys.stderr)
    return False


def delete_snapshot_dirs(paths: list[Path]) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild benchmark artifacts from retained snapshot families plus frozen legacy rows."
    )
    parser.add_argument("--profile", required=True, choices=["interim-20260417", "final"])
    parser.add_argument("--stamp", help="Fresh rerun stamp for --profile final")
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--combined-dir", type=Path, default=DEFAULT_COMBINED_DIR)
    parser.add_argument("--charts-dir", type=Path, default=DEFAULT_CHARTS_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--legacy-csv", type=Path, default=DEFAULT_LEGACY_CSV)
    parser.add_argument("--delete-dropped", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-plots", action="store_true")
    parser.add_argument("--skip-docs", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    prefixes = keep_prefixes(args.profile, args.stamp)

    if not args.snapshot_root.exists():
        print(f"[retained-rebuild] snapshot root not found: {args.snapshot_root}", file=sys.stderr)
        return 2
    if not args.legacy_csv.exists():
        print(f"[retained-rebuild] frozen legacy csv not found: {args.legacy_csv}", file=sys.stderr)
        return 2

    kept, dropped, missing = partition_snapshot_dirs(args.snapshot_root, prefixes)
    keep_path, drop_path = write_inventory(args.state_dir, args.profile, prefixes, kept, dropped, missing)

    print(f"[retained-rebuild] profile={args.profile}")
    print(f"[retained-rebuild] keep inventory: {keep_path}")
    print(f"[retained-rebuild] drop inventory: {drop_path}")
    print(f"[retained-rebuild] kept dirs={len(kept)} dropped dirs={len(dropped)}")
    if missing:
        print(f"[retained-rebuild] missing prefixes={len(missing)}", file=sys.stderr)
        for prefix in missing:
            print(f"  - {prefix}", file=sys.stderr)

    if args.dry_run:
        return 0

    retained_rows = collect_snapshot_rows(kept)
    if not retained_rows:
        print("[retained-rebuild] no retained snapshot summaries found", file=sys.stderr)
        return 1
    frozen_rows = load_frozen_rows(args.legacy_csv)
    rows = retained_rows + frozen_rows
    write_combined(rows, args.combined_dir)
    print(
        f"[retained-rebuild] wrote {args.combined_dir / 'combined.csv'} with {len(rows)} rows "
        f"({len(retained_rows)} retained + {len(frozen_rows)} frozen legacy)"
    )

    if args.delete_dropped:
        delete_snapshot_dirs(dropped)
        print(f"[retained-rebuild] deleted {len(dropped)} dropped snapshot dirs")

    if not args.skip_plots:
        run_optional_step(
            "chart regeneration",
            [sys.executable, "scripts/bench/plot_overhead.py", str(args.combined_dir / "combined.csv"), str(args.charts_dir)],
        )
    if not args.skip_docs:
        docs_ok = run_optional_step("docs rebuild", [sys.executable, "docs/build_docs.py"])
        if not docs_ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
