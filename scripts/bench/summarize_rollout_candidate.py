#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from math import sqrt
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

SCENARIO_ORDER = ("baseline", "candidate", "ordered", "quiet")
CV_REGRESSION_STAGES = ("pods-5", "rollout-2-post", "rollout-5-post")
PHASE_TRACE_STAGES = (
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


def _cv_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    if abs(mean) < 1e-9:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return (sqrt(variance) / abs(mean)) * 100.0


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


def _load_phase_trace(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_experiment_rows(
    experiment_dir: Path,
) -> tuple[dict[str, str], dict[str, dict[str, float]], dict[str, dict[str, object]]]:
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

    stage_metrics = {stage: _aggregate_rows(rows) for stage, rows in grouped.items()}
    phase_traces: dict[str, dict[str, object]] = {}
    snapshot_root = experiment_dir / "snapshots"
    for stage in grouped:
        trace_path = snapshot_root / f"{label_prefix}-{stage}" / "phase-trace.json"
        if not trace_path.exists():
            continue
        trace = _load_phase_trace(trace_path)
        if trace:
            phase_traces[stage] = trace

    return summary, stage_metrics, phase_traces


def _scenario_name(experiment_dir: Path, summary: dict[str, str]) -> str:
    summary_scenario = str(summary.get("scenario", "")).strip()
    if summary_scenario:
        return summary_scenario

    dir_prefix = experiment_dir.name.split("-r", 1)[0]
    if dir_prefix in SCENARIO_ORDER:
        return dir_prefix

    strategy = summary.get("rollout_strategy", "baseline")
    if strategy == "ordered":
        return "ordered"

    steady_quiet = str(summary.get("steady_quiet", "0")).strip().lower()
    if steady_quiet in {"1", "true", "yes", "y"}:
        return "quiet"

    return "baseline"


def _discover_experiments(group_dir: Path) -> list[dict[str, object]]:
    experiments: list[dict[str, object]] = []
    for child in sorted(path for path in group_dir.iterdir() if path.is_dir()):
        summary_path = child / "reports" / "summary.txt"
        csv_path = child / "combined" / "combined.csv"
        if not summary_path.exists() or not csv_path.exists():
            continue
        summary, stages, phase_traces = _load_experiment_rows(child)
        experiments.append(
            {
                "dir": str(child),
                "summary": summary,
                "scenario": _scenario_name(child, summary),
                "stages": stages,
                "phase_traces": phase_traces,
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
            app_values = [float(row["app_mib"]) for row in rows]
            docs_cp_values = [float(row["docs_cp_mib"]) for row in rows]
            host_values = [float(row["host_mib"]) for row in rows]
            memavail_values = [float(row["memavail_delta_mib"]) for row in rows]
            aggregated[scenario][stage] = {
                "runs": float(len(rows)),
                "app_mib": _mean(app_values),
                "app_cv_pct": _cv_pct(app_values),
                "docs_cp_mib": _mean(docs_cp_values),
                "docs_cp_cv_pct": _cv_pct(docs_cp_values),
                "host_mib": _mean(host_values),
                "host_cv_pct": _cv_pct(host_values),
                "memavail_delta_mib": _mean(memavail_values),
                "memavail_delta_cv_pct": _cv_pct(memavail_values),
            }
    return aggregated


def _trace_status_value(trace: dict[str, object], key: str) -> float:
    status = trace.get("status")
    if not isinstance(status, dict):
        return 0.0
    return _to_float(status.get(key))


def _trace_window_status(trace: dict[str, object], bucket: str) -> dict[str, object]:
    window = trace.get("status_window")
    if isinstance(window, dict):
        bucket_value = window.get(bucket)
        if isinstance(bucket_value, dict):
            return bucket_value
    status = trace.get("status")
    if isinstance(status, dict):
        return status
    return {}


def _trace_window_value(trace: dict[str, object], bucket: str, key: str) -> float:
    return _to_float(_trace_window_status(trace, bucket).get(key))


def _trace_window_count(trace: dict[str, object], key: str) -> float:
    window = trace.get("status_window")
    if isinstance(window, dict):
        return _to_float(window.get(key))
    status = trace.get("status")
    return 1.0 if isinstance(status, dict) else 0.0


def _trace_revision_statuses(trace: dict[str, object]) -> list[str]:
    window = trace.get("status_window")
    if isinstance(window, dict):
        raw = window.get("revision_statuses")
        if isinstance(raw, list):
            cleaned = sorted({str(item).strip() for item in raw if str(item).strip()})
            if cleaned:
                return cleaned
    status = trace.get("status")
    if isinstance(status, dict):
        current = str(status.get("revision_status") or "").strip()
        if current:
            return [current]
    return []


def _trace_cri_window(trace: dict[str, object], bucket: str) -> dict[str, object]:
    window = trace.get("cri_window")
    if isinstance(window, dict):
        bucket_value = window.get(bucket)
        if isinstance(bucket_value, dict):
            return bucket_value
    current = trace.get("cri")
    if isinstance(current, dict):
        return current
    return {}


def _trace_cri_value(trace: dict[str, object], bucket: str, key: str) -> float:
    return _to_float(_trace_cri_window(trace, bucket).get(key))


def _trace_cri_count(trace: dict[str, object], key: str) -> float:
    window = trace.get("cri_window")
    if isinstance(window, dict):
        return _to_float(window.get(key))
    current = trace.get("cri")
    return 1.0 if isinstance(current, dict) else 0.0


def _trace_cri_revisions(trace: dict[str, object]) -> list[str]:
    window = trace.get("cri_window")
    if isinstance(window, dict):
        raw = window.get("revisions_seen")
        if isinstance(raw, list):
            cleaned = sorted({str(item).strip() for item in raw if str(item).strip()})
            if cleaned:
                return cleaned
    current = trace.get("cri")
    if isinstance(current, dict):
        raw = current.get("revisions_seen")
        if isinstance(raw, list):
            cleaned = sorted({str(item).strip() for item in raw if str(item).strip()})
            if cleaned:
                return cleaned
    return []


def _scenario_phase_metrics(
    experiments: list[dict[str, object]],
) -> dict[str, dict[str, dict[str, float | list[str]]]]:
    by_scenario_stage: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for experiment in experiments:
        scenario = str(experiment["scenario"])
        traces = experiment.get("phase_traces")
        assert isinstance(traces, dict)
        for stage, trace in traces.items():
            by_scenario_stage[scenario][stage].append(trace)

    aggregated: dict[str, dict[str, dict[str, float | list[str]]]] = {}
    for scenario, stage_traces in by_scenario_stage.items():
        aggregated[scenario] = {}
        for stage, traces in stage_traces.items():
            revision_statuses = sorted(
                {
                    item
                    for trace in traces
                    for item in _trace_revision_statuses(trace)
                }
            )
            aggregated[scenario][stage] = {
                "runs": float(len(traces)),
                "sample_count": _mean([_trace_window_count(trace, "sample_count") for trace in traces]),
                "successful_samples": _mean(
                    [_trace_window_count(trace, "successful_samples") for trace in traces]
                ),
                "failed_samples": _mean([_trace_window_count(trace, "failed_samples") for trace in traces]),
                "ready_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "ready_replicas") for trace in traces]
                ),
                "ready_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "ready_replicas") for trace in traces]
                ),
                "live_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "live_replicas") for trace in traces]
                ),
                "live_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "live_replicas") for trace in traces]
                ),
                "current_revision_ready_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "current_revision_ready_replicas") for trace in traces]
                ),
                "current_revision_ready_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "current_revision_ready_replicas") for trace in traces]
                ),
                "current_revision_live_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "current_revision_live_replicas") for trace in traces]
                ),
                "current_revision_live_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "current_revision_live_replicas") for trace in traces]
                ),
                "old_revision_ready_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "old_revision_ready_replicas") for trace in traces]
                ),
                "old_revision_ready_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "old_revision_ready_replicas") for trace in traces]
                ),
                "old_revision_live_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "old_revision_live_replicas") for trace in traces]
                ),
                "old_revision_live_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "old_revision_live_replicas") for trace in traces]
                ),
                "overlap_ready_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "overlap_ready_replicas") for trace in traces]
                ),
                "overlap_ready_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "overlap_ready_replicas") for trace in traces]
                ),
                "overlap_live_replicas_max": _mean(
                    [_trace_window_value(trace, "max", "overlap_live_replicas") for trace in traces]
                ),
                "overlap_live_replicas_last": _mean(
                    [_trace_window_value(trace, "last", "overlap_live_replicas") for trace in traces]
                ),
                "revision_statuses": revision_statuses,
            }
    return aggregated


def _scenario_cri_phase_metrics(
    experiments: list[dict[str, object]],
) -> dict[str, dict[str, dict[str, float | list[str]]]]:
    by_scenario_stage: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for experiment in experiments:
        scenario = str(experiment["scenario"])
        traces = experiment.get("phase_traces")
        assert isinstance(traces, dict)
        for stage, trace in traces.items():
            if "cri_window" not in trace and "cri" not in trace:
                continue
            by_scenario_stage[scenario][stage].append(trace)

    aggregated: dict[str, dict[str, dict[str, float | list[str]]]] = {}
    for scenario, stage_traces in by_scenario_stage.items():
        aggregated[scenario] = {}
        for stage, traces in stage_traces.items():
            revisions_seen = sorted(
                {
                    item
                    for trace in traces
                    for item in _trace_cri_revisions(trace)
                }
            )
            aggregated[scenario][stage] = {
                "runs": float(len(traces)),
                "sample_count": _mean([_trace_cri_count(trace, "sample_count") for trace in traces]),
                "successful_samples": _mean(
                    [_trace_cri_count(trace, "successful_samples") for trace in traces]
                ),
                "failed_samples": _mean([_trace_cri_count(trace, "failed_samples") for trace in traces]),
                "pod_count_max": _mean(
                    [_trace_cri_value(trace, "max", "pod_count") for trace in traces]
                ),
                "pod_count_last": _mean(
                    [_trace_cri_value(trace, "last", "pod_count") for trace in traces]
                ),
                "current_revision_pods_max": _mean(
                    [_trace_cri_value(trace, "max", "current_revision_pods") for trace in traces]
                ),
                "current_revision_pods_last": _mean(
                    [_trace_cri_value(trace, "last", "current_revision_pods") for trace in traces]
                ),
                "old_revision_pods_max": _mean(
                    [_trace_cri_value(trace, "max", "old_revision_pods") for trace in traces]
                ),
                "old_revision_pods_last": _mean(
                    [_trace_cri_value(trace, "last", "old_revision_pods") for trace in traces]
                ),
                "overlap_pods_max": _mean(
                    [_trace_cri_value(trace, "max", "overlap_pods") for trace in traces]
                ),
                "overlap_pods_last": _mean(
                    [_trace_cri_value(trace, "last", "overlap_pods") for trace in traces]
                ),
                "main_containers_max": _mean(
                    [_trace_cri_value(trace, "max", "main_containers") for trace in traces]
                ),
                "main_containers_last": _mean(
                    [_trace_cri_value(trace, "last", "main_containers") for trace in traces]
                ),
                "current_revision_main_containers_max": _mean(
                    [_trace_cri_value(trace, "max", "current_revision_main_containers") for trace in traces]
                ),
                "current_revision_main_containers_last": _mean(
                    [_trace_cri_value(trace, "last", "current_revision_main_containers") for trace in traces]
                ),
                "old_revision_main_containers_max": _mean(
                    [_trace_cri_value(trace, "max", "old_revision_main_containers") for trace in traces]
                ),
                "old_revision_main_containers_last": _mean(
                    [_trace_cri_value(trace, "last", "old_revision_main_containers") for trace in traces]
                ),
                "overlap_main_containers_max": _mean(
                    [_trace_cri_value(trace, "max", "overlap_main_containers") for trace in traces]
                ),
                "overlap_main_containers_last": _mean(
                    [_trace_cri_value(trace, "last", "overlap_main_containers") for trace in traces]
                ),
                "revisions_seen": revisions_seen,
            }
    return aggregated


def _candidate_scenario(scenario_metrics: dict[str, dict[str, dict[str, float]]]) -> str:
    if "baseline" not in scenario_metrics:
        raise ValueError("candidate group must include baseline and exactly one candidate scenario")
    candidates = sorted(name for name in scenario_metrics if name != "baseline")
    if len(candidates) != 1:
        raise ValueError("candidate group must include baseline and exactly one candidate scenario")
    return candidates[0]


def _build_deltas(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    candidate_scenario: str,
) -> list[dict[str, float | str]]:
    baseline = scenario_metrics.get("baseline", {})
    candidate = scenario_metrics.get(candidate_scenario, {})
    deltas: list[dict[str, float | str]] = []
    for stage in SUMMARY_STAGE_ORDER:
        if stage not in baseline or stage not in candidate:
            continue
        base_metrics = baseline[stage]
        candidate_metrics = candidate[stage]
        item: dict[str, float | str] = {
            "stage": stage,
            "candidate_scenario": candidate_scenario,
            "baseline_app_mib": base_metrics["app_mib"],
            "candidate_app_mib": candidate_metrics["app_mib"],
            "app_delta_mib": candidate_metrics["app_mib"] - base_metrics["app_mib"],
            "app_improvement_mib": base_metrics["app_mib"] - candidate_metrics["app_mib"],
            "baseline_docs_cp_mib": base_metrics["docs_cp_mib"],
            "candidate_docs_cp_mib": candidate_metrics["docs_cp_mib"],
            "docs_cp_delta_mib": candidate_metrics["docs_cp_mib"] - base_metrics["docs_cp_mib"],
            "baseline_host_mib": base_metrics["host_mib"],
            "candidate_host_mib": candidate_metrics["host_mib"],
            "host_delta_mib": candidate_metrics["host_mib"] - base_metrics["host_mib"],
        }
        item[f"{candidate_scenario}_app_mib"] = candidate_metrics["app_mib"]
        item[f"{candidate_scenario}_docs_cp_mib"] = candidate_metrics["docs_cp_mib"]
        item[f"{candidate_scenario}_host_mib"] = candidate_metrics["host_mib"]
        deltas.append(item)
    return deltas


def _build_candidate_gates(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    cri_phase_metrics: dict[str, dict[str, dict[str, float | list[str]]]],
    candidate_scenario: str,
    min_rollout_5_improvement: float,
    min_rollout_5_cri_overlap_reduction: float,
    max_steady_drift: float,
    max_post_drift: float,
) -> list[dict[str, float | str | bool]]:
    baseline = scenario_metrics.get("baseline", {})
    candidate = scenario_metrics.get(candidate_scenario, {})
    if not baseline or not candidate:
        return []

    def stage_metric(scenario: dict[str, dict[str, float]], stage: str, key: str) -> float:
        return float(scenario.get(stage, {}).get(key, 0.0))

    steady_drift = abs(stage_metric(candidate, "pods-5", "app_mib") - stage_metric(baseline, "pods-5", "app_mib"))
    post_drift = abs(
        stage_metric(candidate, "rollout-5-post", "app_mib")
        - stage_metric(baseline, "rollout-5-post", "app_mib")
    )
    rollout_5_improvement = (
        stage_metric(baseline, "rollout-5-during", "app_mib")
        - stage_metric(candidate, "rollout-5-during", "app_mib")
    )
    rollout_2_improvement = (
        stage_metric(baseline, "rollout-2-during", "app_mib")
        - stage_metric(candidate, "rollout-2-during", "app_mib")
    )

    gates: list[dict[str, float | str | bool]] = [
        {
            "gate": "pods-5 steady-state app drift",
            "value_mib": steady_drift,
            "metric": "mib",
            "target": f"<= {max_steady_drift:.1f}",
            "passed": steady_drift <= max_steady_drift,
        },
        {
            "gate": "rollout-5-post app drift",
            "value_mib": post_drift,
            "metric": "mib",
            "target": f"<= {max_post_drift:.1f}",
            "passed": post_drift <= max_post_drift,
        },
        {
            "gate": "rollout-5-during app improvement",
            "value_mib": rollout_5_improvement,
            "metric": "mib",
            "target": f">= {min_rollout_5_improvement:.1f}",
            "passed": rollout_5_improvement >= min_rollout_5_improvement,
        },
        {
            "gate": "rollout-2-during app improvement",
            "value_mib": rollout_2_improvement,
            "metric": "mib",
            "target": "informational",
            "passed": True,
        },
    ]
    baseline_cri = cri_phase_metrics.get("baseline", {})
    candidate_cri = cri_phase_metrics.get(candidate_scenario, {})
    if "rollout-5-during" in baseline_cri and "rollout-5-during" in candidate_cri:
        baseline_overlap = _to_float(
            baseline_cri["rollout-5-during"].get("overlap_pods_max")
        )
        candidate_overlap = _to_float(
            candidate_cri["rollout-5-during"].get("overlap_pods_max")
        )
        overlap_reduction = baseline_overlap - candidate_overlap
        gates.append(
            {
                "gate": "rollout-5-during CRI overlap reduction",
                "value_count": overlap_reduction,
                "metric": "count",
                "target": f">= {min_rollout_5_cri_overlap_reduction:.1f}",
                "passed": overlap_reduction >= min_rollout_5_cri_overlap_reduction,
            }
        )
    return gates


def _build_quiet_gates(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    max_steady_drift: float,
    max_post_drift: float,
    max_app_cv_regression: float,
) -> list[dict[str, float | str | bool]]:
    baseline = scenario_metrics.get("baseline", {})
    quiet = scenario_metrics.get("quiet", {})
    if not baseline or not quiet:
        return []

    def stage_metric(scenario: dict[str, dict[str, float]], stage: str, key: str) -> float:
        return float(scenario.get(stage, {}).get(key, 0.0))

    gates: list[dict[str, float | str | bool]] = [
        {
            "gate": "pods-5 steady-state app drift",
            "value_mib": abs(stage_metric(quiet, "pods-5", "app_mib") - stage_metric(baseline, "pods-5", "app_mib")),
            "metric": "mib",
            "target": f"<= {max_steady_drift:.1f}",
        },
        {
            "gate": "rollout-5-post app drift",
            "value_mib": abs(
                stage_metric(quiet, "rollout-5-post", "app_mib")
                - stage_metric(baseline, "rollout-5-post", "app_mib")
            ),
            "metric": "mib",
            "target": f"<= {max_post_drift:.1f}",
        },
    ]
    for gate in gates:
        gate["passed"] = float(gate["value_mib"]) <= (
            max_steady_drift if gate["gate"] == "pods-5 steady-state app drift" else max_post_drift
        )

    for stage in CV_REGRESSION_STAGES:
        regression = max(
            0.0,
            stage_metric(quiet, stage, "app_cv_pct") - stage_metric(baseline, stage, "app_cv_pct"),
        )
        gates.append(
            {
                "gate": f"{stage} app CV regression",
                "value_pct": regression,
                "metric": "pct",
                "target": f"<= {max_app_cv_regression:.1f}%",
                "passed": regression <= max_app_cv_regression,
            }
        )

    gates.append(
        {
            "gate": "memΔ informational only",
            "value_pct": max(
                0.0,
                stage_metric(quiet, "rollout-5-post", "memavail_delta_cv_pct")
                - stage_metric(baseline, "rollout-5-post", "memavail_delta_cv_pct"),
            ),
            "metric": "pct",
            "target": "informational",
            "passed": True,
        }
    )
    return gates


def _build_gates(
    scenario_metrics: dict[str, dict[str, dict[str, float]]],
    cri_phase_metrics: dict[str, dict[str, dict[str, float | list[str]]]],
    candidate_scenario: str,
    min_rollout_5_improvement: float,
    min_rollout_5_cri_overlap_reduction: float,
    max_steady_drift: float,
    max_post_drift: float,
    max_app_cv_regression: float,
) -> list[dict[str, float | str | bool]]:
    if candidate_scenario != "quiet":
        return _build_candidate_gates(
            scenario_metrics,
            cri_phase_metrics,
            candidate_scenario,
            min_rollout_5_improvement,
            min_rollout_5_cri_overlap_reduction,
            max_steady_drift,
            max_post_drift,
        )
    return _build_quiet_gates(
        scenario_metrics,
        max_steady_drift,
        max_post_drift,
        max_app_cv_regression,
    )


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
    phase_metrics: dict[str, dict[str, dict[str, float | list[str]]]],
    cri_phase_metrics: dict[str, dict[str, dict[str, float | list[str]]]],
    candidate_scenario: str,
    deltas: list[dict[str, float | str]],
    gates: list[dict[str, float | str | bool]],
) -> str:
    lines = [
        f"group_dir={group_dir}",
        f"experiments={len(experiments)}",
    ]

    scenario_rows: list[tuple[str, ...]] = []
    for scenario in ("baseline", candidate_scenario):
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
                    f"{metrics['app_cv_pct']:.1f}",
                    f"{metrics['docs_cp_mib']:.1f}",
                    f"{metrics['docs_cp_cv_pct']:.1f}",
                    f"{metrics['host_mib']:.1f}",
                    f"{metrics['memavail_delta_mib']:.1f}",
                )
            )
    if scenario_rows:
        lines.extend(
            [
                "",
                _table(
                    ("scenario", "stage", "runs", "app", "app_cv%", "docs_cp", "docs_cp_cv%", "host", "memΔ"),
                    scenario_rows,
                ),
            ]
        )

    phase_rows: list[tuple[str, ...]] = []
    for scenario in ("baseline", candidate_scenario):
        stages = phase_metrics.get(scenario, {})
        for stage in PHASE_TRACE_STAGES:
            metrics = stages.get(stage)
            if not metrics:
                continue
            phase_rows.append(
                (
                    scenario,
                    stage,
                    str(int(float(metrics["runs"]))),
                    f"{float(metrics['successful_samples']):.1f}",
                    f"{float(metrics['live_replicas_max']):.1f}/{float(metrics['live_replicas_last']):.1f}",
                    f"{float(metrics['current_revision_live_replicas_max']):.1f}/{float(metrics['current_revision_live_replicas_last']):.1f}",
                    f"{float(metrics['old_revision_live_replicas_max']):.1f}/{float(metrics['old_revision_live_replicas_last']):.1f}",
                    f"{float(metrics['overlap_live_replicas_max']):.1f}/{float(metrics['overlap_live_replicas_last']):.1f}",
                    ",".join(str(item) for item in metrics.get("revision_statuses", [])) or "-",
                )
            )
    if phase_rows:
        lines.extend(
            [
                "",
                _table(
                    (
                        "scenario",
                        "stage",
                        "runs",
                        "samples",
                        "live max/last",
                        "cur_live max/last",
                        "old_live max/last",
                        "overlap max/last",
                        "rev_status",
                    ),
                    phase_rows,
                ),
            ]
        )

    cri_phase_rows: list[tuple[str, ...]] = []
    for scenario in ("baseline", candidate_scenario):
        stages = cri_phase_metrics.get(scenario, {})
        for stage in PHASE_TRACE_STAGES:
            metrics = stages.get(stage)
            if not metrics:
                continue
            cri_phase_rows.append(
                (
                    scenario,
                    stage,
                    str(int(float(metrics["runs"]))),
                    f"{float(metrics['successful_samples']):.1f}",
                    f"{float(metrics['current_revision_pods_max']):.1f}/{float(metrics['current_revision_pods_last']):.1f}",
                    f"{float(metrics['old_revision_pods_max']):.1f}/{float(metrics['old_revision_pods_last']):.1f}",
                    f"{float(metrics['overlap_pods_max']):.1f}/{float(metrics['overlap_pods_last']):.1f}",
                    f"{float(metrics['current_revision_main_containers_max']):.1f}/{float(metrics['current_revision_main_containers_last']):.1f}",
                    f"{float(metrics['old_revision_main_containers_max']):.1f}/{float(metrics['old_revision_main_containers_last']):.1f}",
                    f"{float(metrics['overlap_main_containers_max']):.1f}/{float(metrics['overlap_main_containers_last']):.1f}",
                    ",".join(str(item) for item in metrics.get("revisions_seen", [])) or "-",
                )
            )
    if cri_phase_rows:
        lines.extend(
            [
                "",
                _table(
                    (
                        "scenario",
                        "stage",
                        "runs",
                        "samples",
                        "cri_cur_pods max/last",
                        "cri_old_pods max/last",
                        "cri_overlap_pods max/last",
                        "cri_cur_main max/last",
                        "cri_old_main max/last",
                        "cri_overlap_main max/last",
                        "cri_revisions",
                    ),
                    cri_phase_rows,
                ),
            ]
        )

    delta_rows: list[tuple[str, ...]] = []
    delta_headers = ("stage", "baseline_app", f"{candidate_scenario}_app", "appΔ", "docs_cpΔ", "hostΔ")
    for item in deltas:
        delta_rows.append(
            (
                str(item["stage"]),
                f"{float(item['baseline_app_mib']):.1f}",
                f"{float(item['candidate_app_mib']):.1f}",
                f"{float(item['app_delta_mib']):.1f}",
                f"{float(item['docs_cp_delta_mib']):.1f}",
                f"{float(item['host_delta_mib']):.1f}",
            )
        )
    if delta_rows:
        lines.extend(
            [
                "",
                _table(delta_headers, delta_rows),
            ]
        )

    gate_rows = []
    for item in gates:
        value = ""
        metric = str(item.get("metric", "mib"))
        if "value_mib" in item:
            value = f"{float(item['value_mib']):.1f}"
        elif "value_count" in item:
            value = f"{float(item['value_count']):.1f}"
        elif "value_pct" in item:
            value = f"{float(item['value_pct']):.1f}%"
        gate_rows.append(
            (
                str(item["gate"]),
                value,
                str(item["target"]),
                "yes" if bool(item["passed"]) else "no",
            )
        )
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
        description="Summarize grouped rollout candidate experiments under one output root."
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
        default=15.0,
        help="Minimum rollout-5-during app-memory improvement to treat as a pass.",
    )
    parser.add_argument(
        "--min-rollout-5-cri-overlap-reduction",
        type=float,
        default=1.0,
        help="Minimum rollout-5-during CRI overlap reduction to treat as a pass when CRI traces are available.",
    )
    parser.add_argument(
        "--max-steady-drift",
        type=float,
        default=3.0,
        help="Maximum allowed pods-5 app-memory drift between baseline and the candidate scenario.",
    )
    parser.add_argument(
        "--max-post-drift",
        type=float,
        default=3.0,
        help="Maximum allowed rollout-5-post app-memory drift between baseline and the candidate scenario.",
    )
    parser.add_argument(
        "--max-app-cv-regression",
        type=float,
        default=5.0,
        help="Maximum allowed app-memory CV regression for quiet candidate stages.",
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
    phase_metrics = _scenario_phase_metrics(experiments)
    cri_phase_metrics = _scenario_cri_phase_metrics(experiments)
    try:
        candidate_scenario = _candidate_scenario(scenario_metrics)
    except ValueError as exc:
        parser.error(str(exc))

    deltas = _build_deltas(scenario_metrics, candidate_scenario)
    gates = _build_gates(
        scenario_metrics,
        cri_phase_metrics,
        candidate_scenario,
        args.min_rollout_5_improvement,
        args.min_rollout_5_cri_overlap_reduction,
        args.max_steady_drift,
        args.max_post_drift,
        args.max_app_cv_regression,
    )
    payload = {
        "group_dir": str(args.group_dir),
        "candidate_scenario": candidate_scenario,
        "experiments": experiments,
        "scenario_metrics": scenario_metrics,
        "phase_metrics": phase_metrics,
        "cri_phase_metrics": cri_phase_metrics,
        "deltas": deltas,
        "gates": gates,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            _render_text(
                args.group_dir,
                experiments,
                scenario_metrics,
                phase_metrics,
                cri_phase_metrics,
                candidate_scenario,
                deltas,
                gates,
            ),
            end="",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
