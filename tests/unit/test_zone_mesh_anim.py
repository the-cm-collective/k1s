# ruff: noqa: S603
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "zone_mesh_anim.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def test_zone_mesh_inspect_scales_density() -> None:
    low = json.loads(run_cli("inspect", "--profile", "low").stdout)
    medium = json.loads(run_cli("inspect", "--profile", "medium").stdout)
    high = json.loads(run_cli("inspect", "--profile", "high").stdout)

    assert low["node_count"] < medium["node_count"] < high["node_count"]
    assert low["link_count"] < medium["link_count"] < high["link_count"]
    assert low["query_target_count"] < medium["query_target_count"] < high["query_target_count"]
    assert low["chunk_group_count"] < medium["chunk_group_count"] < high["chunk_group_count"]
    assert low["counts_by_kind"]["dc"] == 2
    assert medium["counts_by_kind"]["dc"] == 2
    assert high["counts_by_kind"]["dc"] == 2


def test_zone_mesh_render_gif_smoke(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        return

    run_cli(
        "render",
        "--profile",
        "low",
        "--format",
        "gif",
        "--frame-limit",
        "4",
        "--output-dir",
        str(tmp_path),
    )

    output = tmp_path / "zone_mesh_density_low.gif"
    assert output.exists()
    assert output.stat().st_size > 0
