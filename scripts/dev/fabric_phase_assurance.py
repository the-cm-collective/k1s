#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ae.fabric.phase_assurance import assess_fabric_phases  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fabric_phase_assurance.py",
        description="Assess k1s F-phase roadmap evidence and dependency gates.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="JSON evidence file. Accepts direct phase evidence or a prior assurance report.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the full JSON report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload: dict[str, Any] | None = None
    if args.evidence:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("--evidence must contain a JSON object")
    report = assess_fabric_phases(payload)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for phase_id in report["phase_order"]:
        phase = report["phases"][phase_id]
        gate = phase["gate"]
        blockers = ",".join(gate["blocked_by"]) if gate["blocked_by"] else "-"
        print(
            f"{phase_id}: status={phase['status']} gate_ready={str(gate['ready']).lower()} "
            f"blocked_by={blockers} missing={len(phase['missing'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
