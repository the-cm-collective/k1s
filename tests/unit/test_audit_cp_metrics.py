from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT_CP_METRICS = ROOT / "scripts" / "bench" / "audit_cp_metrics.py"


def _write_csv(path: Path) -> None:
    fieldnames = [
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
    rows = [
        {
            "label": "cri-clean5-run1+cri+containerd-idle",
            "mode": "k1s",
            "backend": "cri",
            "timestamp": "20260417-120000",
            "control_plane_pss_kb": "242000",
            "controller_pss_kb": "83600",
            "ingress_pss_kb": "0",
            "runtime_pss_kb": "158400",
            "k3s_control_plane_pss_kb": "0",
            "app_mem_bytes": "0",
            "host_system_cgroups_bytes": str(878 * 1024 * 1024),
            "mem_available_delta_bytes": str(177 * 1024 * 1024),
        },
        {
            "label": "cri-clean5-run2+cri+containerd-idle",
            "mode": "k1s",
            "backend": "cri",
            "timestamp": "20260417-121000",
            "control_plane_pss_kb": "243000",
            "controller_pss_kb": "83800",
            "ingress_pss_kb": "0",
            "runtime_pss_kb": "159200",
            "k3s_control_plane_pss_kb": "0",
            "app_mem_bytes": "0",
            "host_system_cgroups_bytes": str(879 * 1024 * 1024),
            "mem_available_delta_bytes": str(178 * 1024 * 1024),
        },
        {
            "label": "k1nd-old-idle",
            "mode": "k1s",
            "backend": "docker",
            "timestamp": "20260415-120000",
            "control_plane_pss_kb": "133120",
            "controller_pss_kb": "91200",
            "ingress_pss_kb": "43800",
            "runtime_pss_kb": "1260",
            "k3s_control_plane_pss_kb": "0",
            "app_mem_bytes": "0",
            "host_system_cgroups_bytes": str(2542 * 1024 * 1024),
            "mem_available_delta_bytes": str(168 * 1024 * 1024),
        },
        {
            "label": "k3d-old-idle",
            "mode": "k3s",
            "backend": "docker",
            "timestamp": "20260415-120500",
            "control_plane_pss_kb": "807936",
            "controller_pss_kb": "0",
            "ingress_pss_kb": "0",
            "runtime_pss_kb": "241000",
            "k3s_control_plane_pss_kb": "560128",
            "app_mem_bytes": str(87 * 1024 * 1024),
            "host_system_cgroups_bytes": str(3605 * 1024 * 1024),
            "mem_available_delta_bytes": str(209 * 1024 * 1024),
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_audit_cp_metrics_reports_shadow_variants(tmp_path: Path) -> None:
    csv_path = tmp_path / "combined.csv"
    _write_csv(csv_path)

    result = subprocess.run(
        [
            sys.executable,
            str(AUDIT_CP_METRICS),
            str(csv_path),
            "--family",
            "cri-clean5",
            "--family",
            "k1nd-old",
            "--family",
            "k3d-old",
            "--stage",
            "idle",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 3

    cri = next(item for item in payload if item["family"] == "cri-clean5")
    assert cri["rows"] == 2
    assert round(cri["controller_mib"], 1) == 81.7
    assert round(cri["controller_plus_ingress_mib"], 1) == 81.7
    assert round(cri["docs_cp_mib"], 1) == 81.7
    assert round(cri["raw_cp_mib"], 1) == 236.8

    k1nd = next(item for item in payload if item["family"] == "k1nd-old")
    assert round(k1nd["controller_mib"], 1) == 89.1
    assert round(k1nd["ingress_mib"], 1) == 42.8
    assert round(k1nd["controller_plus_ingress_mib"], 1) == 131.8
    assert round(k1nd["docs_cp_mib"], 1) == 131.8
    assert round(k1nd["raw_cp_mib"], 1) == 130.0

    k3d = next(item for item in payload if item["family"] == "k3d-old")
    assert round(k3d["controller_mib"], 1) == 0.0
    assert round(k3d["controller_plus_ingress_mib"], 1) == 0.0
    assert round(k3d["docs_cp_mib"], 1) == 547.0
    assert round(k3d["raw_cp_mib"], 1) == 789.0
