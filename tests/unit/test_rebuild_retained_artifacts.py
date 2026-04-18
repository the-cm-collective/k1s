from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench" / "rebuild_retained_artifacts.py"
LEGACY_CSV = ROOT / "scripts" / "bench" / "data" / "legacy_20260203_frozen.csv"


def _load_module():
    spec = importlib.util.spec_from_file_location("rebuild_retained_artifacts", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_summary(path: Path, label: str, timestamp: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "label": label,
            "mode": "k1s",
            "backend": "podman",
            "oci_runtime": "crun",
            "timestamp": timestamp,
        },
        "process_totals_kb": {"pss_kb": 12345},
        "overhead": {
            "pss_kb_control_plane": 12345,
            "pss_kb_total_overhead": 12345,
            "pss_kb_controller": 12000,
            "pss_kb_ingress": 0,
            "pss_kb_runtime": 345,
            "pss_kb_k3s_control_plane": 0,
            "host_system_cgroups_bytes": 987654321,
        },
        "containers": {"app_mem_bytes": 111, "system_mem_bytes": 0},
        "mem_available": {"before_bytes": 200, "after_bytes": 150, "delta_bytes": 50},
    }
    (path / "summary.json").write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_frozen_csv_matches_schema_and_row_count() -> None:
    module = _load_module()
    with LEGACY_CSV.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == module.FIELDNAMES
    assert len(rows) == 40


def test_keep_prefix_profiles_cover_expected_families() -> None:
    module = _load_module()

    interim = module.keep_prefixes("interim-20260417", None)
    assert len(interim) == 8
    assert "r20260417-overlap-smoke-rootless" in interim
    assert "r20260417-cri-runc-baseline-clean5-run3+cri+containerd" in interim

    final = module.keep_prefixes("final", "r20260420-fullretest")
    assert len(final) == 7
    assert "r20260420-fullretest+podman+rootless+cg2" in final
    assert "r20260420-fullretest+cri-runc-verify-run3+cri+containerd" in final


def test_dry_run_writes_keep_drop_inventory(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    combined_dir = tmp_path / "combined"
    state_dir = tmp_path / "state"
    legacy_csv = tmp_path / "legacy.csv"
    legacy_csv.write_text(LEGACY_CSV.read_text(encoding="utf-8"), encoding="utf-8")

    _write_summary(
        snapshot_root / "r20260417-overlap-smoke-rootless-idle" / "20260417-000001",
        "r20260417-overlap-smoke-rootless+podman+crun+rootless+cg2-idle",
        "20260417-000001",
    )
    _write_summary(
        snapshot_root / "r20260413-old-invalid-idle" / "20260413-000001",
        "r20260413-old-invalid-idle",
        "20260413-000001",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile",
            "interim-20260417",
            "--snapshot-root",
            str(snapshot_root),
            "--combined-dir",
            str(combined_dir),
            "--state-dir",
            str(state_dir),
            "--legacy-csv",
            str(legacy_csv),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    keep_text = (state_dir / "bench-retained.keep.txt").read_text(encoding="utf-8")
    drop_text = (state_dir / "bench-retained.drop.txt").read_text(encoding="utf-8")
    assert "r20260417-overlap-smoke-rootless-idle" in keep_text
    assert "r20260413-old-invalid-idle" in drop_text
    assert not (combined_dir / "combined.csv").exists()
