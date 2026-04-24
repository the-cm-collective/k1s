from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "f0n_nvidia_validate.py"


def test_f0n_validate_plan_emits_stable_artifact_shape(tmp_path) -> None:
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--run-id",
            "review-run",
            "--runs-dir",
            str(tmp_path),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["run_id"] == "review-run"
    assert payload["inventory"]["nodes"].endswith("review-run/ae/nodes.json")
    assert payload["inventory"]["plan"].endswith("review-run/plan.json")
    assert payload["inventory"]["summary"].endswith("review-run/summary.json")
    assert [cell["name"] for cell in payload["cells"]] == [
        "cell-a-single",
        "cell-b-single",
        "cell-ab-pp2-ray",
        "cell-ab-pp2-mp",
    ]
    for cell in payload["cells"]:
        artifacts = cell["artifacts"]
        assert artifacts["status_initial"].endswith("status-initial.json")
        assert artifacts["events_initial"].endswith("events-initial.txt")
        assert artifacts["status_reapplied"].endswith("status-reapplied.json")
        assert artifacts["teardown"].endswith("teardown.txt")
