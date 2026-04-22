#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
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

RUNTIME_GROUP_KEYS = (
    "containerd",
    "containerd_shim",
    "conmon",
    "podman",
    "passt",
    "slirp4netns",
    "dockerd",
    "other_runtime",
)

HOST_TOP_LIMIT = 5


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


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _collect_summary_paths(inputs: list[str]) -> list[Path]:
    paths: dict[Path, None] = {}
    for raw in inputs:
        matched = glob.glob(raw)
        candidates = matched or [raw]
        for item in candidates:
            path = Path(item)
            if path.is_file() and path.name == "summary.json":
                paths[path.resolve()] = None
                continue
            if path.is_dir() and (path / "summary.json").exists():
                paths[(path / "summary.json").resolve()] = None
                continue
            if path.is_dir():
                for found in path.rglob("summary.json"):
                    paths[found.resolve()] = None
    return sorted(paths.keys())


def _host_top(summary: dict) -> list[dict[str, object]]:
    return list((summary.get("overhead") or {}).get("host_system_cgroups_top") or [])


def _runtime_groups(summary: dict) -> dict[str, int]:
    groups = dict((summary.get("overhead") or {}).get("runtime_process_groups") or {})
    return {key: int(groups.get(key, 0) or 0) for key in RUNTIME_GROUP_KEYS}


def _runtime_group_stats(summary: dict) -> dict[str, dict[str, float]]:
    raw_stats = dict((summary.get("overhead") or {}).get("runtime_process_group_stats") or {})
    stats: dict[str, dict[str, float]] = {}
    for key in RUNTIME_GROUP_KEYS:
        group = dict(raw_stats.get(key) or {})
        pss_kb = int(group.get("pss_kb", 0) or 0)
        count = int(group.get("count", 0) or 0)
        mean_pss_kb = int(group.get("mean_pss_kb", 0) or 0)
        if count <= 0 and pss_kb > 0:
            count = 1
        if mean_pss_kb <= 0 and count > 0:
            mean_pss_kb = int(round(pss_kb / count))
        stats[key] = {
            "count": float(count),
            "pss_mib": _kib_to_mib(pss_kb),
            "mean_pss_mib": _kib_to_mib(mean_pss_kb),
        }
    return stats


def _container_counts(summary: dict) -> dict[str, int]:
    containers = dict(summary.get("containers") or {})
    return {
        "app": int(containers.get("app_container_count", 0) or 0),
        "system": int(containers.get("system_container_count", 0) or 0),
        "total": int(containers.get("total_container_count", 0) or 0),
    }


def _shim_baseline_row(summary_rows: list[dict[str, object]]) -> dict[str, float]:
    if not summary_rows:
        return {"mib": 0.0, "count": 0.0, "mean_mib": 0.0}
    idle_row = _aggregate_group("_idle", "idle", summary_rows)
    runtime_groups = dict(idle_row.get("runtime_groups_mib") or {})
    runtime_counts = dict(idle_row.get("runtime_group_counts") or {})
    runtime_mean_mib = dict(idle_row.get("runtime_group_mean_mib") or {})
    return {
        "mib": float(runtime_groups.get("containerd_shim", 0.0)),
        "count": float(runtime_counts.get("containerd_shim", 0.0)),
        "mean_mib": float(runtime_mean_mib.get("containerd_shim", 0.0)),
    }


def _attach_shim_baseline(
    row: dict[str, object],
    baseline: dict[str, float],
) -> dict[str, object]:
    runtime_groups = dict(row.get("runtime_groups_mib") or {})
    runtime_counts = dict(row.get("runtime_group_counts") or {})
    shim_total_mib = float(runtime_groups.get("containerd_shim", 0.0))
    shim_total_count = float(runtime_counts.get("containerd_shim", 0.0))
    shim_base_mib = float(baseline.get("mib", 0.0))
    shim_base_count = float(baseline.get("count", 0.0))
    shim_excess_mib = max(0.0, shim_total_mib - shim_base_mib)
    shim_excess_count = max(0.0, shim_total_count - shim_base_count)
    shim_excess_mean_mib = shim_excess_mib / shim_excess_count if shim_excess_count > 0 else 0.0
    row["containerd_shim_baseline_mib"] = shim_base_mib
    row["containerd_shim_baseline_count"] = shim_base_count
    row["containerd_shim_baseline_mean_mib"] = float(baseline.get("mean_mib", 0.0))
    row["containerd_shim_excess_mib"] = shim_excess_mib
    row["containerd_shim_excess_count"] = shim_excess_count
    row["containerd_shim_excess_mean_mib"] = shim_excess_mean_mib
    return row


def _load_groups(
    inputs: list[str],
    families: list[str],
    stages: set[str] | None,
) -> list[dict[str, object]]:
    by_group_all: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    by_group_selected: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for summary_path in _collect_summary_paths(inputs):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = summary.get("meta") or {}
        label = str(meta.get("label") or "")
        stage = _stage_name(label)
        if stage is None:
            continue
        for family in families:
            if label.startswith(family):
                by_group_all[(family, stage)].append(summary)
                if stages is None or stage in stages:
                    by_group_selected[(family, stage)].append(summary)
                break

    shim_baselines = {
        family: _shim_baseline_row(by_group_all.get((family, "idle"), [])) for family in families
    }

    results: list[dict[str, object]] = []
    for family in families:
        for stage in STAGES:
            summaries = by_group_selected.get((family, stage))
            if not summaries:
                continue
            row = _aggregate_group(family, stage, summaries)
            results.append(_attach_shim_baseline(row, shim_baselines.get(family, {})))
    return results


def _aggregate_group(family: str, stage: str, summaries: list[dict[str, object]]) -> dict[str, object]:
    meta = (summaries[0].get("meta") or {}) if summaries else {}
    timestamps = sorted(str((summary.get("meta") or {}).get("timestamp") or "") for summary in summaries)
    runtime_groups_by_key: dict[str, list[float]] = {key: [] for key in RUNTIME_GROUP_KEYS}
    runtime_group_counts_by_key: dict[str, list[float]] = {key: [] for key in RUNTIME_GROUP_KEYS}
    runtime_group_mean_by_key: dict[str, list[float]] = {key: [] for key in RUNTIME_GROUP_KEYS}
    host_rows: dict[str, dict[str, object]] = {}
    app_container_counts: list[float] = []

    for summary in summaries:
        groups = _runtime_groups(summary)
        group_stats = _runtime_group_stats(summary)
        counts = _container_counts(summary)
        app_container_counts.append(float(counts["app"]))
        for key in RUNTIME_GROUP_KEYS:
            runtime_groups_by_key[key].append(_kib_to_mib(groups.get(key, 0)))
            runtime_group_counts_by_key[key].append(float(group_stats.get(key, {}).get("count", 0.0)))
            runtime_group_mean_by_key[key].append(
                float(group_stats.get(key, {}).get("mean_pss_mib", 0.0))
            )
        for row in _host_top(summary):
            path = str(row.get("path") or "")
            if not path:
                continue
            entry = host_rows.setdefault(
                path,
                {
                    "path": path,
                    "slice_kind": str(row.get("slice_kind") or ""),
                    "bytes": [],
                },
            )
            entry["bytes"].append(int(row.get("bytes") or 0))

    top_host_services = []
    for path, entry in host_rows.items():
        mean_bytes = _mean([float(v) for v in entry["bytes"]])
        top_host_services.append(
            {
                "path": path,
                "slice_kind": str(entry.get("slice_kind") or ""),
                "bytes": int(round(mean_bytes)),
                "mib": round(mean_bytes / (1024.0 * 1024.0), 2),
            }
        )
    top_host_services.sort(key=lambda item: (-int(item.get("bytes") or 0), str(item.get("path") or "")))
    top_host_services = top_host_services[:HOST_TOP_LIMIT]

    row: dict[str, object] = {
        "family": family,
        "stage": stage,
        "rows": len(summaries),
        "mode": str(meta.get("mode") or ""),
        "first_timestamp": timestamps[0] if timestamps else "",
        "last_timestamp": timestamps[-1] if timestamps else "",
        "runtime_mib": _mean(
            [_kib_to_mib((summary.get("overhead") or {}).get("pss_kb_runtime", 0)) for summary in summaries]
        ),
        "app_mib": _mean(
            [_bytes_to_mib((summary.get("containers") or {}).get("app_mem_bytes", 0)) for summary in summaries]
        ),
        "host_system_mib": _mean(
            [
                _bytes_to_mib((summary.get("overhead") or {}).get("host_system_cgroups_bytes", 0))
                for summary in summaries
            ]
        ),
        "app_container_count": _mean(app_container_counts),
        "runtime_groups_mib": {
            key: _mean(values) for key, values in runtime_groups_by_key.items()
        },
        "runtime_group_counts": {
            key: _mean(values) for key, values in runtime_group_counts_by_key.items()
        },
        "runtime_group_mean_mib": {
            key: _mean(values) for key, values in runtime_group_mean_by_key.items()
        },
        "top_host_services": top_host_services,
    }
    return row


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit runtime attribution from benchmark snapshot summary.json outputs."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Experiment roots, snapshot roots, or summary.json globs to read.",
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
        ("app_container_count", "app_cnt"),
        ("runtime_mib", "rt"),
        ("app_mib", "app"),
        ("host_system_mib", "host"),
        ("containerd_mib", "containerd"),
        ("containerd_shim_mib", "shim"),
        ("containerd_shim_count", "shim_cnt"),
        ("containerd_shim_mean_mib", "shim_mean"),
        ("containerd_shim_baseline_mib", "shim_base"),
        ("containerd_shim_baseline_count", "shim_bcnt"),
        ("containerd_shim_excess_mib", "shim_ex"),
        ("containerd_shim_excess_count", "shim_excnt"),
        ("conmon_mib", "conmon"),
        ("podman_mib", "podman"),
        ("passt_mib", "passt"),
        ("slirp4netns_mib", "slirp"),
        ("dockerd_mib", "dockerd"),
        ("other_runtime_mib", "other_rt"),
    )
    for row in rows:
        runtime_groups = dict(row.get("runtime_groups_mib") or {})
        runtime_counts = dict(row.get("runtime_group_counts") or {})
        runtime_mean_mib = dict(row.get("runtime_group_mean_mib") or {})
        row["containerd_mib"] = float(runtime_groups.get("containerd", 0.0))
        row["containerd_shim_mib"] = float(runtime_groups.get("containerd_shim", 0.0))
        row["containerd_shim_count"] = float(runtime_counts.get("containerd_shim", 0.0))
        row["containerd_shim_mean_mib"] = float(runtime_mean_mib.get("containerd_shim", 0.0))
        row["containerd_shim_baseline_mib"] = float(row.get("containerd_shim_baseline_mib", 0.0))
        row["containerd_shim_baseline_count"] = float(row.get("containerd_shim_baseline_count", 0.0))
        row["containerd_shim_excess_mib"] = float(row.get("containerd_shim_excess_mib", 0.0))
        row["containerd_shim_excess_count"] = float(row.get("containerd_shim_excess_count", 0.0))
        row["conmon_mib"] = float(runtime_groups.get("conmon", 0.0))
        row["podman_mib"] = float(runtime_groups.get("podman", 0.0))
        row["passt_mib"] = float(runtime_groups.get("passt", 0.0))
        row["slirp4netns_mib"] = float(runtime_groups.get("slirp4netns", 0.0))
        row["dockerd_mib"] = float(runtime_groups.get("dockerd", 0.0))
        row["other_runtime_mib"] = float(runtime_groups.get("other_runtime", 0.0))

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

    print()
    for row in rows:
        print(f"{row['family']} {row['stage']} host_top")
        for item in row.get("top_host_services") or []:
            print(f"  {float(item.get('mib', 0.0)):7.1f} MiB  {item.get('path', '')}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    rows = _load_groups(args.inputs, args.families, set(args.stages) if args.stages else None)
    if not rows:
        print("no matching summaries found")
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
