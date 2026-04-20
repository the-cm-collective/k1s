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

SUMMARY_STAGE_ORDER = (
    "pods-5",
    "rollout-2-during",
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


def _parse_summary(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _to_float(raw: object) -> float:
    try:
        return float(raw or 0)
    except Exception:
        return 0.0


def _kib_to_mib(raw: object) -> float:
    return _to_float(raw) / 1024.0


def _bytes_to_mib(raw: object) -> float:
    return _to_float(raw) / (1024.0 * 1024.0)


def _docs_cp_mib(row: dict[str, str]) -> float:
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


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _aggregate_rows(rows: list[dict[str, str]]) -> dict[str, float]:
    return {
        "rows": float(len(rows)),
        "app_mib": _mean([_bytes_to_mib(row.get("app_mem_bytes")) for row in rows]),
        "docs_cp_mib": _mean([_docs_cp_mib(row) for row in rows]),
        "host_mib": _mean([_bytes_to_mib(row.get("host_system_cgroups_bytes")) for row in rows]),
        "memavail_delta_mib": _mean(
            [_bytes_to_mib(row.get("mem_available_delta_bytes")) for row in rows]
        ),
    }


def _load_experiment_rows(experiment_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, float]]]:
    summary_path = experiment_dir / "reports" / "summary.txt"
    csv_path = experiment_dir / "combined" / "combined.csv"
    summary = _parse_summary(summary_path)
    label_prefix = summary.get("label", "")
    if not label_prefix:
        raise ValueError(f"missing label in {summary_path}")

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            label = str(row.get("label", ""))
            if not label.startswith(label_prefix):
                continue
            stage = _stage_name(label)
            if stage is None:
                continue
            grouped[stage].append(row)

    if not grouped:
        raise ValueError(f"no benchmark rows for label {label_prefix} in {csv_path}")

    return summary, {stage: _aggregate_rows(rows) for stage, rows in grouped.items()}


def _scenario_name(summary: dict[str, str]) -> str:
    strategy = summary.get("rollout_strategy", "baseline")
    return "ordered" if strategy == "ordered" else "baseline"


def _discover_experiments(group_dir: Path) -> list[dict[str, object]]:
    experiments: list[dict[str, object]] = []
    for child in sorted(path for path in group_dir.iterdir() if path.is_dir()):
        summary_path = child / "reports" / "summary.txt"
        csv_path = child / "combined" / "combined.csv"
        if not summary_path.exists() or not csv_path.exists():
            continue
        summary, stages = _load_experiment_rows(child)
        experiments.append(
            {
                "dir": str(child),
                "summary": summary,
                "scenario": _scenario_name(summary),
                "stages": stages,
            }
        )
    return experiments


def _scenario_stage_metrics(
    experiments: list[dict[str, object]],
) -> dict[str, dict[str, dict[str, float]]]:
    by_scenario_stage: dict[str, dict[str, list[dict[str, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for experiment in experiments:
        scenario = str(experiment["scenario"])
        stages = experiment["stages"]
        assert isinstance(stages, dict)
        for stage, metrics in stages.items():
            by_scenario_stage[scenario][stage].append(metrics)

    aggregated: dict[str, dict[str, dict[str, float]]] = {}
    for scenario, stage_rows in by_scenario_stage.items():
        aggregated[scenario] = {}
        for stage, rows in stage_rows.items():
            aggregated[scenario][stage] = {
                "runs": float(len(rows)),
                "app_mib": _mean([float(row["app_mib"]) for row in rows]),
                "docs_cp_mib": _mean([float(row["docs_cp_mib"]) for row in rows]),
                "host_mib": _mean([float(row["host_mib"]) for row in rows]),
                "memavail_delta_mib": _mean([float(row["memavail_delta_mib"]) for row in rows]),
            }
    return aggregated


def _build_deltas(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
) -> list[dict[str, float | str]]:
    baseline = scenario_metrics.get("baseline", {})
    ordered = scenario_metrics.get("ordered", {})
    deltas: list[dict[str, float | str]] = []
    for stage in SUMMARY_STAGE_ORDER:
        if stage not in baseline or stage not in ordered:
            continue
        base_metrics = baseline[stage]
        ordered_metrics = ordered[stage]
        deltas.append(
            {
                "stage": stage,
                "baseline_app_mib": base_metrics["app_mib"],
                "ordered_app_mib": ordered_metrics["app_mib"],
                "app_delta_mib": ordered_metrics["app_mib"] - base_metrics["app_mib"],
                "app_improvement_mib": base_metrics["app_mib"] - ordered_metrics["app_mib"],
                "baseline_docs_cp_mib": base_metrics["docs_cp_mib"],
                "ordered_docs_cp_mib": ordered_metrics["docs_cp_mib"],
                "docs_cp_delta_mib": ordered_metrics["docs_cp_mib"] - base_metrics["docs_cp_mib"],
                "baseline_host_mib": base_metrics["host_mib"],
                "ordered_host_mib": ordered_metrics["host_mib"],
                "host_delta_mib": ordered_metrics["host_mib"] - base_metrics["host_mib"],
            }
        )
    return deltas


def _build_gates(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    min_rollout_5_improvement: float,
    max_steady_drift: float,
    max_post_drift: float,
) -> list[dict[str, float | str | bool]]:
    baseline = scenario_metrics.get("baseline", {})
    ordered = scenario_metrics.get("ordered", {})
    if not baseline or not ordered:
        return []

    def stage_metric(scenario: dict[str, dict[str, float]], stage: str, key: str) -> float:
        return float(scenario.get(stage, {}).get(key, 0.0))

    steady_drift = abs(stage_metric(ordered, "pods-5", "app_mib") - stage_metric(baseline, "pods-5", "app_mib"))
    post_drift = abs(
        stage_metric(ordered, "rollout-5-post", "app_mib")
        - stage_metric(baseline, "rollout-5-post", "app_mib")
    )
    rollout_5_improvement = (
        stage_metric(baseline, "rollout-5-during", "app_mib")
        - stage_metric(ordered, "rollout-5-during", "app_mib")
    )
    rollout_2_improvement = (
        stage_metric(baseline, "rollout-2-during", "app_mib")
        - stage_metric(ordered, "rollout-2-during", "app_mib")
    )

    return [
        {
            "gate": "pods-5 steady-state app drift",
            "value_mib": steady_drift,
            "target": f"<= {max_steady_drift:.1f}",
            "passed": steady_drift <= max_steady_drift,
        },
        {
            "gate": "rollout-5-post app drift",
            "value_mib": post_drift,
            "target": f"<= {max_post_drift:.1f}",
            "passed": post_drift <= max_post_drift,
        },
        {
            "gate": "rollout-5-during app improvement",
            "value_mib": rollout_5_improvement,
            "target": f">= {min_rollout_5_improvement:.1f}",
            "passed": rollout_5_improvement >= min_rollout_5_improvement,
        },
        {
            "gate": "rollout-2-during app improvement",
            "value_mib": rollout_2_improvement,
            "target": "informational",
            "passed": True,
        },
    ]


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    lines = [
        " ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)),
        " ".join("-" * widths[idx] for idx in range(len(headers))),
    ]
    for row in rows:
        lines.append(" ".join(value.ljust(widths[idx]) for idx, value in enumerate(row)))
    return "\n".join(lines)


def _render_text(
    group_dir: Path,
    experiments: list[dict[str, object]],
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    deltas: list[dict[str, float | str]],
    gates: list[dict[str, float | str | bool]],
) -> str:
    lines = [
        f"group_dir={group_dir}",
        f"experiments={len(experiments)}",
    ]

    scenario_rows: list[tuple[str, ...]] = []
    for scenario in ("baseline", "ordered"):
        stages = scenario_metrics.get(scenario, {})
        for stage in SUMMARY_STAGE_ORDER:
            metrics = stages.get(stage)
            if not metrics:
                continue
            scenario_rows.append(
                (
                    scenario,
                    stage,
                    str(int(metrics["runs"])),
                    f"{metrics['app_mib']:.1f}",
                    f"{metrics['docs_cp_mib']:.1f}",
                    f"{metrics['host_mib']:.1f}",
                    f"{metrics['memavail_delta_mib']:.1f}",
                )
            )
    if scenario_rows:
        lines.extend(
            [
                "",
                _table(
                    ("scenario", "stage", "runs", "app", "docs_cp", "host", "memΔ"),
                    scenario_rows,
                ),
            ]
        )

    delta_rows = [
        (
            str(item["stage"]),
            f"{float(item['baseline_app_mib']):.1f}",
            f"{float(item['ordered_app_mib']):.1f}",
            f"{float(item['app_improvement_mib']):.1f}",
            f"{float(item['docs_cp_delta_mib']):.1f}",
            f"{float(item['host_delta_mib']):.1f}",
        )
        for item in deltas
    ]
    if delta_rows:
        lines.extend(
            [
                "",
                _table(
                    ("stage", "baseline_app", "ordered_app", "app_gain", "docs_cpΔ", "hostΔ"),
                    delta_rows,
                ),
            ]
        )

    gate_rows = [
        (
            str(item["gate"]),
            f"{float(item['value_mib']):.1f}",
            str(item["target"]),
            "yes" if bool(item["passed"]) else "no",
        )
        for item in gates
    ]
    if gate_rows:
        lines.extend(
            [
                "",
                _table(("gate", "value", "target", "pass"), gate_rows),
            ]
        )

    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize grouped CRI rollout candidate experiments under one output root."
    )
    parser.add_argument(
        "group_dir",
        type=Path,
        help="Candidate group directory containing experiment subdirectories.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text report.",
    )
    parser.add_argument(
        "--min-rollout-5-improvement",
        type=float,
        default=30.0,
        help="Minimum rollout-5-during app-memory improvement to treat as a pass.",
    )
    parser.add_argument(
        "--max-steady-drift",
        type=float,
        default=3.0,
        help="Maximum allowed pods-5 app-memory drift between baseline and ordered.",
    )
    parser.add_argument(
        "--max-post-drift",
        type=float,
        default=3.0,
        help="Maximum allowed rollout-5-post app-memory drift between baseline and ordered.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.group_dir.exists():
        parser.error(f"missing group dir: {args.group_dir}")

    experiments = _discover_experiments(args.group_dir)
    if not experiments:
        parser.error(f"no experiment subdirectories found in {args.group_dir}")

    scenario_metrics = _scenario_stage_metrics(experiments)
    if "baseline" not in scenario_metrics or "ordered" not in scenario_metrics:
        parser.error("candidate group must include both baseline and ordered experiment runs")

    deltas = _build_deltas(scenario_metrics)
    gates = _build_gates(
        scenario_metrics,
        args.min_rollout_5_improvement,
        args.max_steady_drift,
        args.max_post_drift,
    )
    payload = {
        "group_dir": str(args.group_dir),
        "experiments": experiments,
        "scenario_metrics": scenario_metrics,
        "deltas": deltas,
        "gates": gates,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render_text(args.group_dir, experiments, scenario_metrics, deltas, gates), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
