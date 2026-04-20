from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


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
    rows: list[dict[str, str]],
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
                "steady_quiet=0" if rollout_strategy == "baseline" else "steady_quiet=1",
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

    ordered_rollout = payload["scenario_metrics"]["ordered"]["rollout-5-during"]
    baseline_rollout = payload["scenario_metrics"]["baseline"]["rollout-5-during"]
    assert round(ordered_rollout["app_mib"], 1) == 102.5
    assert round(baseline_rollout["app_mib"], 1) == 161.0

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
