from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ae.fabric.phase_assurance import PHASE_REQUIREMENTS, assess_fabric_phases

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "fabric_phase_assurance.py"


def _complete(*phase_ids: str) -> dict[str, dict[str, bool]]:
    return {
        phase_id: dict.fromkeys(PHASE_REQUIREMENTS[phase_id], True)
        for phase_id in phase_ids
    }


def test_f3_is_blocked_until_f1_and_f2_are_present() -> None:
    report = assess_fabric_phases(_complete("F0", "F3"))

    assert report["phases"]["F3"]["status"] == "present"
    assert report["phases"]["F3"]["gate"]["ready"] is False
    assert report["phases"]["F3"]["gate"]["blocked_by"] == ["F1", "F2"]


def test_f0_ready_does_not_make_f1_or_f2_ready() -> None:
    report = assess_fabric_phases(_complete("F0"))

    assert report["phases"]["F0"]["gate"]["ready"] is True
    assert report["phases"]["F1"]["status"] == "missing"
    assert report["phases"]["F1"]["gate"]["ready"] is False
    assert "typed_link_topology" in report["phases"]["F1"]["missing"]
    assert report["phases"]["F2"]["gate"]["blocked_by"] == ["F1"]


def test_later_phases_keep_dependency_order() -> None:
    report = assess_fabric_phases(_complete("F0", "F1", "F2", "F4", "F5"))

    assert report["phases"]["F2"]["gate"]["ready"] is True
    assert report["phases"]["F4"]["status"] == "present"
    assert report["phases"]["F4"]["gate"]["ready"] is False
    assert report["phases"]["F4"]["gate"]["blocked_by"] == ["F3"]
    assert report["phases"]["F5"]["gate"]["blocked_by"] == ["F3"]


def test_f0n_nvidia_subtrack_does_not_substitute_for_f0() -> None:
    report = assess_fabric_phases(_complete("F0n-nvidia-dev", "F1"))

    assert report["phases"]["F0n-nvidia-dev"]["gate"]["ready"] is True
    assert report["phases"]["F0"]["status"] == "missing"
    assert report["phases"]["F1"]["status"] == "present"
    assert report["phases"]["F1"]["gate"]["ready"] is False
    assert report["phases"]["F1"]["gate"]["blocked_by"] == ["F0"]


def test_assurance_script_emits_json_report(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_complete("F0", "F1", "F2")), encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--evidence", str(evidence_path), "--json"],
        check=True,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert report["api_version"] == "k1s.fabric.phase-assurance/v1"
    assert report["phases"]["F2"]["gate"]["ready"] is True
    assert report["phases"]["F3"]["gate"]["blocked_by"] == []
    assert report["phases"]["F3"]["gate"]["ready"] is False
