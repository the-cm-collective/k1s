#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

STAGES = (
    "idle",
    "pods-1",
    "pods-5",
    "pods-10",
    "rollout-2-during",
    "rollout-2-during-warm",
    "rollout-2-post",
    "rollout-5-during",
    "rollout-5-during-warm",
    "rollout-5-post",
)


def _stage_name(label: str) -> str | None:
    for stage in sorted(STAGES, key=len, reverse=True):
        if label.endswith(f"-{stage}"):
            return stage
    return None


def _to_float(raw: object) -> float:
    try:
        return float(raw or 0)
    except Exception:
        return 0.0


def _kib_to_mib(raw: object) -> float:
    return _to_float(raw) / 1024.0


def _bytes_to_mib(raw: object) -> float:
    return _to_float(raw) / (1024.0 * 1024.0)


def _cp_docs_current_mib(row: dict[str, str]) -> float:
    mode = str(row.get("mode", "")).lower()
    if mode == "k3s":
        k3s_cp = _to_float(row.get("k3s_control_plane_pss_kb"))
        if k3s_cp > 0:
            return k3s_cp / 1024.0

    controller = _to_float(row.get("controller_pss_kb"))
    ingress = _to_float(row.get("ingress_pss_kb"))
    if controller > 0 or ingress > 0:
        return (controller + ingress) / 1024.0

    return _kib_to_mib(row.get("control_plane_pss_kb"))


def _row_metrics(row: dict[str, str]) -> dict[str, float]:
    controller_mib = _kib_to_mib(row.get("controller_pss_kb"))
    ingress_mib = _kib_to_mib(row.get("ingress_pss_kb"))
    runtime_mib = _kib_to_mib(row.get("runtime_pss_kb"))
    return {
        "controller_mib": controller_mib,
        "ingress_mib": ingress_mib,
        "runtime_mib": runtime_mib,
        "controller_plus_ingress_mib": controller_mib + ingress_mib,
        "docs_cp_mib": _cp_docs_current_mib(row),
        "raw_cp_mib": _kib_to_mib(row.get("control_plane_pss_kb")),
        "k3s_cp_mib": _kib_to_mib(row.get("k3s_control_plane_pss_kb")),
        "app_mib": _bytes_to_mib(row.get("app_mem_bytes")),
        "host_system_mib": _bytes_to_mib(row.get("host_system_cgroups_bytes")),
        "memavail_delta_mib": _bytes_to_mib(row.get("mem_available_delta_bytes")),
    }


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _aggregate_rows(rows: list[dict[str, str]]) -> dict[str, object]:
    mode = rows[0].get("mode", "") if rows else ""
    backend = rows[0].get("backend", "") if rows else ""
    timestamps = sorted(str(row.get("timestamp", "")) for row in rows)
    metrics = [_row_metrics(row) for row in rows]
    keys = (
        "controller_mib",
        "ingress_mib",
        "runtime_mib",
        "controller_plus_ingress_mib",
        "docs_cp_mib",
        "raw_cp_mib",
        "k3s_cp_mib",
        "app_mib",
        "host_system_mib",
        "memavail_delta_mib",
    )
    result: dict[str, object] = {
        "rows": len(rows),
        "mode": mode,
        "backend": backend,
        "first_timestamp": timestamps[0] if timestamps else "",
        "last_timestamp": timestamps[-1] if timestamps else "",
    }
    for key in keys:
        result[key] = _mean([float(item[key]) for item in metrics])
    return result


def _load_groups(
    csv_path: Path,
    families: list[str],
    stages: set[str] | None,
) -> list[dict[str, object]]:
    by_group: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            label = str(row.get("label", ""))
            stage = _stage_name(label)
            if stage is None:
                continue
            if stages is not None and stage not in stages:
                continue
            for family in families:
                if label.startswith(family):
                    by_group[(family, stage)].append(row)
                    break

    results: list[dict[str, object]] = []
    for family in families:
        for stage in STAGES:
            rows = by_group.get((family, stage))
            if not rows:
                continue
            aggregated = _aggregate_rows(rows)
            aggregated["family"] = family
            aggregated["stage"] = stage
            results.append(aggregated)
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit control-plane metric variants from combined benchmark CSV rows."
        )
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=Path("combined/combined.csv"),
        help="Path to combined benchmark CSV (default: combined/combined.csv)",
    )
    parser.add_argument(
        "--family",
        action="append",
        dest="families",
        required=True,
        help="Label family/prefix to include (repeatable).",
    )
    parser.add_argument(
        "--stage",
        action="append",
        dest="stages",
        choices=STAGES,
        help="Restrict to one or more benchmark stages.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a plain-text table.",
    )
    return parser


def _print_table(rows: list[dict[str, object]]) -> None:
    columns = (
        ("family", "family"),
        ("stage", "stage"),
        ("rows", "rows"),
        ("mode", "mode"),
        ("controller_mib", "ctrl"),
        ("ingress_mib", "ing"),
        ("runtime_mib", "rt"),
        ("controller_plus_ingress_mib", "ctrl+ing"),
        ("docs_cp_mib", "docs_cp"),
        ("raw_cp_mib", "raw_cp"),
        ("app_mib", "app"),
        ("host_system_mib", "host"),
        ("memavail_delta_mib", "memΔ"),
    )
    widths: dict[str, int] = {}
    for key, label in columns:
        widths[key] = len(label)
        for row in rows:
            value = row.get(key, "")
            if isinstance(value, float):
                rendered = f"{value:.1f}"
            else:
                rendered = str(value)
            widths[key] = max(widths[key], len(rendered))

    header = " ".join(label.ljust(widths[key]) for key, label in columns)
    print(header)
    print(" ".join("-" * widths[key] for key, _label in columns))
    for row in rows:
        rendered_cols: list[str] = []
        for key, _label in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                rendered = f"{value:.1f}".rjust(widths[key])
            elif isinstance(value, int):
                rendered = str(value).rjust(widths[key])
            else:
                rendered = str(value).ljust(widths[key])
            rendered_cols.append(rendered)
        print(" ".join(rendered_cols))


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.csv_path.exists():
        parser.error(f"missing CSV: {args.csv_path}")

    stages = set(args.stages) if args.stages else None
    rows = _load_groups(args.csv_path, args.families, stages)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
