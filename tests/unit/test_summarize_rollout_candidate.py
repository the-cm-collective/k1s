from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench" / "summarize_rollout_candidate.py"

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


def _write_experiment(
    group_dir: Path,
    name: str,
    label: str,
    rollout_strategy: str,
    steady_quiet: int,
    rows: list[dict[str, str]],
    phase_traces: dict[str, dict[str, object]] | None = None,
) -> None:
    exp_dir = group_dir / name
    (exp_dir / "combined").mkdir(parents=True, exist_ok=True)
    (exp_dir / "reports").mkdir(parents=True, exist_ok=True)
    (exp_dir / "reports" / "summary.txt").write_text(
        "\n".join(
            [
                "lane=cri",
                f"label={label}",
                "app=echo",
                "manifest=state/bench/apply/echo.yaml",
                f"steady_quiet={steady_quiet}",
                f"rollout_strategy={rollout_strategy}",
                f"combined_csv={exp_dir}/combined/combined.csv",
                f"charts_dir={exp_dir}/charts",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with (exp_dir / "combined" / "combined.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    for stage, trace in (phase_traces or {}).items():
        trace_dir = exp_dir / "snapshots" / f"{label}-{stage}"
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "phase-trace.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _row(label: str, app_mib: float, docs_cp_mib: float, host_mib: float) -> dict[str, str]:
    controller_kib = int(docs_cp_mib * 1024)
    host_bytes = int(host_mib * 1024 * 1024)
    app_bytes = int(app_mib * 1024 * 1024)
    return {
        "label": label,
        "mode": "k1s",
        "backend": "cri",
        "oci_runtime": "runc",
        "timestamp": "20260418-160000",
        "process_pss_kb": "0",
        "control_plane_pss_kb": str(controller_kib),
        "overhead_pss_kb_total": str(controller_kib),
        "controller_pss_kb": str(controller_kib),
        "ingress_pss_kb": "0",
        "runtime_pss_kb": "0",
        "k3s_control_plane_pss_kb": "0",
        "app_mem_bytes": str(app_bytes),
        "system_mem_bytes": "0",
        "host_system_cgroups_bytes": str(host_bytes),
        "mem_available_before_bytes": str(1024 * 1024 * 1024),
        "mem_available_after_bytes": str(900 * 1024 * 1024),
        "mem_available_delta_bytes": str(124 * 1024 * 1024),
    }


def _phase_trace(
    *,
    label: str,
    stage: str,
    desired: int,
    ready: int,
    live: int,
    current_live: int,
    old_live: int,
    overlap_live: int,
    revision_status: str = "progressing",
    window_max: dict[str, object] | None = None,
    window_last: dict[str, object] | None = None,
    sample_count: int | None = None,
) -> dict[str, object]:
    status = {
        "app_name": "default/echo",
        "current_revision_live_replicas": current_live,
        "current_revision_ready_replicas": min(current_live, ready),
        "desired_replicas": desired,
        "image": "localhost/demo-blue:latest",
        "live_replicas": live,
        "old_revision_live_replicas": old_live,
        "old_revision_ready_replicas": min(old_live, ready),
        "overlap_live_replicas": overlap_live,
        "overlap_ready_replicas": min(overlap_live, ready),
        "ready_replicas": ready,
        "revision": 7,
        "revision_status": revision_status,
    }
    trace = {
        "app": "echo",
        "backend": "cri",
        "captured_at": "2026-04-20T15:00:00+00:00",
        "label": label,
        "schema_version": 1,
        "stage": stage,
        "status": status,
        "target_replicas": desired,
    }
    if window_max is not None or window_last is not None or sample_count is not None:
        max_status = dict(status)
        max_status.update(window_max or {})
        last_status = dict(status)
        last_status.update(window_last or {})
        trace["status"] = last_status
        trace["status_window"] = {
            "duration_seconds": 30.0,
            "interval_seconds": 1.0,
            "sample_count": int(sample_count or 1),
            "successful_samples": int(sample_count or 1),
            "failed_samples": 0,
            "max": max_status,
            "last": last_status,
            "revision_statuses": sorted(
                {
                    str(max_status.get("revision_status") or "").strip(),
                    str(last_status.get("revision_status") or "").strip(),
                }
                - {""}
            ),
        }
    return trace


def test_summarize_rollout_candidate_reports_expected_deltas_and_gates(tmp_path: Path) -> None:
    group_dir = tmp_path / "candidate"
    baseline_label_1 = "r20260418-cri+exp+candidate-baseline-r1"
    ordered_label_1 = "r20260418-cri+exp+candidate-ordered-r1"
    baseline_label_2 = "r20260418-cri+exp+candidate-baseline-r2"
    ordered_label_2 = "r20260418-cri+exp+candidate-ordered-r2"

    _write_experiment(
        group_dir,
        "baseline-r1",
        baseline_label_1,
        "baseline",
        0,
        [
            _row(f"{baseline_label_1}-pods-5", 94.0, 85.0, 790.0),
            _row(f"{baseline_label_1}-rollout-2-during", 64.0, 88.0, 791.0),
            _row(f"{baseline_label_1}-rollout-2-post", 28.7, 85.0, 780.0),
            _row(f"{baseline_label_1}-rollout-5-during", 160.0, 88.0, 796.0),
            _row(f"{baseline_label_1}-rollout-5-during-warm", 150.0, 88.0, 796.0),
            _row(f"{baseline_label_1}-rollout-5-post", 71.8, 85.0, 792.0),
        ],
    )
    _write_experiment(
        group_dir,
        "ordered-r1",
        ordered_label_1,
        "ordered",
        1,
        [
            _row(f"{ordered_label_1}-pods-5", 94.5, 86.0, 792.0),
            _row(f"{ordered_label_1}-rollout-2-during", 50.0, 87.0, 783.0),
            _row(f"{ordered_label_1}-rollout-2-post", 28.7, 85.0, 782.0),
            _row(f"{ordered_label_1}-rollout-5-during", 100.0, 87.0, 794.0),
            _row(f"{ordered_label_1}-rollout-5-during-warm", 72.0, 87.0, 793.0),
            _row(f"{ordered_label_1}-rollout-5-post", 71.8, 85.0, 793.0),
        ],
    )
    _write_experiment(
        group_dir,
        "baseline-r2",
        baseline_label_2,
        "baseline",
        0,
        [
            _row(f"{baseline_label_2}-pods-5", 95.0, 86.0, 792.0),
            _row(f"{baseline_label_2}-rollout-2-during", 66.0, 89.0, 790.0),
            _row(f"{baseline_label_2}-rollout-2-post", 28.7, 86.0, 781.0),
            _row(f"{baseline_label_2}-rollout-5-during", 162.0, 89.0, 797.0),
            _row(f"{baseline_label_2}-rollout-5-during-warm", 152.0, 89.0, 797.0),
            _row(f"{baseline_label_2}-rollout-5-post", 71.8, 86.0, 793.0),
        ],
    )
    _write_experiment(
        group_dir,
        "ordered-r2",
        ordered_label_2,
        "ordered",
        1,
        [
            _row(f"{ordered_label_2}-pods-5", 94.2, 87.0, 795.0),
            _row(f"{ordered_label_2}-rollout-2-during", 50.4, 87.0, 784.0),
            _row(f"{ordered_label_2}-rollout-2-post", 28.7, 86.0, 783.0),
            _row(f"{ordered_label_2}-rollout-5-during", 105.0, 88.0, 803.0),
            _row(f"{ordered_label_2}-rollout-5-during-warm", 105.0, 88.0, 802.0),
            _row(f"{ordered_label_2}-rollout-5-post", 71.8, 86.0, 798.0),
        ],
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(group_dir), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert len(payload["experiments"]) == 4
    assert payload["candidate_scenario"] == "ordered"

    ordered_rollout = payload["scenario_metrics"]["ordered"]["rollout-5-during"]
    baseline_rollout = payload["scenario_metrics"]["baseline"]["rollout-5-during"]
    assert round(ordered_rollout["app_mib"], 1) == 102.5
    assert round(baseline_rollout["app_mib"], 1) == 161.0
    assert round(ordered_rollout["app_cv_pct"], 1) == 2.4

    rollout_gate = next(
        gate for gate in payload["gates"] if gate["gate"] == "rollout-5-during app improvement"
    )
    assert rollout_gate["passed"] is True
    assert round(float(rollout_gate["value_mib"]), 1) == 58.5

    steady_gate = next(
        gate for gate in payload["gates"] if gate["gate"] == "pods-5 steady-state app drift"
    )
    assert steady_gate["passed"] is True
    assert round(float(steady_gate["value_mib"]), 1) == 0.2


def test_summarize_rollout_candidate_reports_quiet_candidate_cv_gates(tmp_path: Path) -> None:
    group_dir = tmp_path / "quiet-candidate"
    baseline_label = "r20260420-rootless+exp+candidate-baseline-r1"
    quiet_label = "r20260420-rootless+exp+candidate-quiet-r1"
    baseline_label_2 = "r20260420-rootless+exp+candidate-baseline-r2"
    quiet_label_2 = "r20260420-rootless+exp+candidate-quiet-r2"

    _write_experiment(
        group_dir,
        "baseline-r1",
        baseline_label,
        "baseline",
        0,
        [
            _row(f"{baseline_label}-pods-5", 95.0, 82.0, 790.0),
            _row(f"{baseline_label}-rollout-2-post", 28.7, 82.5, 789.0),
            _row(f"{baseline_label}-rollout-5-post", 71.8, 83.0, 791.0),
        ],
    )
    _write_experiment(
        group_dir,
        "quiet-r1",
        quiet_label,
        "baseline",
        1,
        [
            _row(f"{quiet_label}-pods-5", 95.5, 83.0, 788.0),
            _row(f"{quiet_label}-rollout-2-post", 28.9, 83.1, 788.5),
            _row(f"{quiet_label}-rollout-5-post", 72.0, 83.2, 789.0),
        ],
    )
    _write_experiment(
        group_dir,
        "baseline-r2",
        baseline_label_2,
        "baseline",
        0,
        [
            _row(f"{baseline_label_2}-pods-5", 95.1, 82.3, 790.0),
            _row(f"{baseline_label_2}-rollout-2-post", 28.8, 82.6, 789.0),
            _row(f"{baseline_label_2}-rollout-5-post", 71.9, 83.1, 791.0),
        ],
    )
    _write_experiment(
        group_dir,
        "quiet-r2",
        quiet_label_2,
        "baseline",
        1,
        [
            _row(f"{quiet_label_2}-pods-5", 95.4, 83.4, 788.0),
            _row(f"{quiet_label_2}-rollout-2-post", 28.8, 83.0, 788.5),
            _row(f"{quiet_label_2}-rollout-5-post", 72.1, 83.5, 789.0),
        ],
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(group_dir), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["candidate_scenario"] == "quiet"

    quiet_pods = payload["scenario_metrics"]["quiet"]["pods-5"]
    assert quiet_pods["app_mib"] == pytest.approx(95.45, abs=0.05)
    assert round(quiet_pods["app_cv_pct"], 1) == 0.1

    cv_gate = next(gate for gate in payload["gates"] if gate["gate"] == "rollout-5-post app CV regression")
    assert cv_gate["passed"] is True
    assert round(float(cv_gate["value_pct"]), 1) == 0.0

    drift_gate = next(gate for gate in payload["gates"] if gate["gate"] == "rollout-5-post app drift")
    assert drift_gate["passed"] is True
    assert round(float(drift_gate["value_mib"]), 1) == 0.2


def test_summarize_rollout_candidate_reports_phase_overlap_metrics(tmp_path: Path) -> None:
    group_dir = tmp_path / "candidate-phase"
    baseline_label_1 = "r20260420-cri+exp+candidate-baseline-r1"
    baseline_label_2 = "r20260420-cri+exp+candidate-baseline-r2"
    ordered_label_1 = "r20260420-cri+exp+candidate-ordered-r1"
    ordered_label_2 = "r20260420-cri+exp+candidate-ordered-r2"

    _write_experiment(
        group_dir,
        "baseline-r1",
        baseline_label_1,
        "baseline",
        0,
        [
            _row(f"{baseline_label_1}-rollout-5-during", 106.0, 84.0, 800.0),
            _row(f"{baseline_label_1}-rollout-5-post", 71.8, 83.0, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{baseline_label_1}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=2,
                live=5,
                current_live=1,
                old_live=4,
                overlap_live=4,
            ),
            "rollout-5-post": _phase_trace(
                label=f"{baseline_label_1}-rollout-5-post",
                stage="rollout-5-post",
                desired=5,
                ready=5,
                live=5,
                current_live=5,
                old_live=0,
                overlap_live=0,
                revision_status="ready",
            ),
        },
    )
    _write_experiment(
        group_dir,
        "baseline-r2",
        baseline_label_2,
        "baseline",
        0,
        [
            _row(f"{baseline_label_2}-rollout-5-during", 105.0, 84.0, 801.0),
            _row(f"{baseline_label_2}-rollout-5-post", 71.8, 83.1, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{baseline_label_2}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=2,
                live=5,
                current_live=1,
                old_live=4,
                overlap_live=4,
            ),
        },
    )
    _write_experiment(
        group_dir,
        "ordered-r1",
        ordered_label_1,
        "ordered",
        1,
        [
            _row(f"{ordered_label_1}-rollout-5-during", 96.0, 84.0, 799.0),
            _row(f"{ordered_label_1}-rollout-5-post", 71.8, 83.1, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{ordered_label_1}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=3,
                live=5,
                current_live=2,
                old_live=3,
                overlap_live=3,
            ),
        },
    )
    _write_experiment(
        group_dir,
        "ordered-r2",
        ordered_label_2,
        "ordered",
        1,
        [
            _row(f"{ordered_label_2}-rollout-5-during", 95.0, 84.0, 799.0),
            _row(f"{ordered_label_2}-rollout-5-post", 71.8, 83.2, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{ordered_label_2}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=4,
                live=5,
                current_live=3,
                old_live=2,
                overlap_live=2,
            ),
        },
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(group_dir), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    baseline_phase = payload["phase_metrics"]["baseline"]["rollout-5-during"]
    ordered_phase = payload["phase_metrics"]["ordered"]["rollout-5-during"]
    assert baseline_phase["overlap_live_replicas_max"] == pytest.approx(4.0, abs=0.01)
    assert baseline_phase["overlap_live_replicas_last"] == pytest.approx(4.0, abs=0.01)
    assert ordered_phase["overlap_live_replicas_max"] == pytest.approx(2.5, abs=0.01)
    assert ordered_phase["current_revision_live_replicas_max"] == pytest.approx(2.5, abs=0.01)


def test_summarize_rollout_candidate_prefers_phase_window_max_and_last_metrics(tmp_path: Path) -> None:
    group_dir = tmp_path / "candidate-phase-window"
    baseline_label = "r20260420-cri+exp+candidate-baseline-r1"
    ordered_label = "r20260420-cri+exp+candidate-ordered-r1"

    _write_experiment(
        group_dir,
        "baseline-r1",
        baseline_label,
        "baseline",
        0,
        [
            _row(f"{baseline_label}-rollout-5-during", 120.0, 84.0, 800.0),
            _row(f"{baseline_label}-rollout-5-post", 71.8, 83.0, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{baseline_label}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=1,
                live=1,
                current_live=1,
                old_live=0,
                overlap_live=0,
                window_max={
                    "live_replicas": 5,
                    "current_revision_live_replicas": 2,
                    "old_revision_live_replicas": 3,
                    "overlap_live_replicas": 3,
                },
                window_last={
                    "live_replicas": 1,
                    "current_revision_live_replicas": 1,
                    "old_revision_live_replicas": 0,
                    "overlap_live_replicas": 0,
                },
                sample_count=30,
            ),
        },
    )
    _write_experiment(
        group_dir,
        "ordered-r1",
        ordered_label,
        "ordered",
        1,
        [
            _row(f"{ordered_label}-rollout-5-during", 100.0, 84.0, 799.0),
            _row(f"{ordered_label}-rollout-5-post", 71.8, 83.1, 790.0),
        ],
        phase_traces={
            "rollout-5-during": _phase_trace(
                label=f"{ordered_label}-rollout-5-during",
                stage="rollout-5-during",
                desired=5,
                ready=1,
                live=1,
                current_live=1,
                old_live=0,
                overlap_live=0,
                window_max={
                    "live_replicas": 4,
                    "current_revision_live_replicas": 3,
                    "old_revision_live_replicas": 1,
                    "overlap_live_replicas": 1,
                },
                window_last={
                    "live_replicas": 1,
                    "current_revision_live_replicas": 1,
                    "old_revision_live_replicas": 0,
                    "overlap_live_replicas": 0,
                },
                sample_count=30,
            ),
        },
    )

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(group_dir), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    baseline_phase = payload["phase_metrics"]["baseline"]["rollout-5-during"]
    ordered_phase = payload["phase_metrics"]["ordered"]["rollout-5-during"]
    assert baseline_phase["successful_samples"] == pytest.approx(30.0, abs=0.01)
    assert baseline_phase["overlap_live_replicas_max"] == pytest.approx(3.0, abs=0.01)
    assert baseline_phase["overlap_live_replicas_last"] == pytest.approx(0.0, abs=0.01)
    assert ordered_phase["current_revision_live_replicas_max"] == pytest.approx(3.0, abs=0.01)
    assert ordered_phase["current_revision_live_replicas_last"] == pytest.approx(1.0, abs=0.01)
