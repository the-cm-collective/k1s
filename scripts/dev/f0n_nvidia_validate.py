#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import gpu_guest_passthrough_validate as egpu_validate

DEFAULT_RUNS_DIR = ROOT / "runs"


@dataclass(frozen=True)
class CellLane:
    name: str
    manifest: Path


CELL_LANES = (
    CellLane("cell-a-single", ROOT / "specs/examples/inference/cell-a-single.yaml"),
    CellLane("cell-b-single", ROOT / "specs/examples/inference/cell-b-single.yaml"),
    CellLane("cell-ab-pp2-ray", ROOT / "specs/examples/inference/cell-ab-pp2-ray.yaml"),
    CellLane("cell-ab-pp2-mp", ROOT / "specs/examples/inference/cell-ab-pp2-mp.yaml"),
)


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("f0n-%Y%m%dT%H%M%SZ")


def build_plan(*, run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_root = runs_dir / run_id
    egpu_config = egpu_validate.ValidationConfig(
        run_id=run_id,
        runs_dir=runs_dir,
        guest_ip=None,
        vm_name=None,
        inventory=None,
        ssh_user=egpu_validate.DEFAULT_SSH_USER,
        ssh_key=str(Path.home() / ".ssh" / "id_rsa"),
        guest_repo=egpu_validate.DEFAULT_GUEST_REPO,
        expected_gpu=egpu_validate.DEFAULT_EXPECTED_GPU,
        min_vram_gib=egpu_validate.DEFAULT_MIN_VRAM_GIB,
        expected_pci_bus_id=None,
        runtime_handler=egpu_validate.DEFAULT_RUNTIME_HANDLER,
        compute_image=egpu_validate.DEFAULT_COMPUTE_IMAGE,
        compute_success_signal=egpu_validate.DEFAULT_COMPUTE_SUCCESS_SIGNAL,
        execution_model=egpu_validate.DEFAULT_EXECUTION_MODEL,
    )
    egpu_plan = egpu_validate.build_plan(egpu_config)
    cells: list[dict[str, Any]] = []
    for lane in CELL_LANES:
        cell_root = run_root / "cells" / lane.name
        cells.append(
            {
                "name": lane.name,
                "manifest": str(lane.manifest),
                "artifacts": {
                    "apply": str(cell_root / "apply.txt"),
                    "status_initial": str(cell_root / "status-initial.json"),
                    "events_initial": str(cell_root / "events-initial.txt"),
                    "delete": str(cell_root / "delete.txt"),
                    "reapply": str(cell_root / "reapply.txt"),
                    "status_reapplied": str(cell_root / "status-reapplied.json"),
                    "events_reapplied": str(cell_root / "events-reapplied.txt"),
                    "teardown": str(cell_root / "teardown.txt"),
                },
            }
        )
    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "inventory": {
            "nodes": str(run_root / "ae" / "nodes.json"),
            "plan": str(run_root / "plan.json"),
            "summary": str(run_root / "summary.json"),
        },
        "execution_hosts": [
            {
                "host_id": "core-a",
                "node_id": "core-a--hub",
                "execution_model": "linux_guest_passthrough",
            },
            {
                "host_id": "edge-b",
                "node_id": "edge-b--gpu-1",
                "execution_model": "host_native",
            },
        ],
        "checks": {
            "egpu_passthrough_validate": egpu_plan["artifacts"]["summary"],
            "egpu_attach": egpu_plan["artifacts"]["attach"],
            "egpu_cri_runtime": egpu_plan["artifacts"]["cri_runtime"],
            "egpu_compute_smoke": egpu_plan["artifacts"]["compute_smoke"],
        },
        "phases": [
            {
                "name": "egpu_passthrough_validate",
                "execution_model": "linux_guest_passthrough",
                "artifacts": egpu_plan["artifacts"],
            },
            {
                "name": "cell_validation",
                "execution_model": "mixed_lane",
                "cell_count": len(CELL_LANES),
            },
        ],
        "cells": cells,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="f0n_nvidia_validate.py",
        description="Collect operator-run F0n Nvidia lane evidence for the physical A/B baseline.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    plan = sub.add_parser("plan", help="Print the run plan and artifact layout.")
    plan.add_argument("--run-id", default=default_run_id())
    plan.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    plan.add_argument("--json", action="store_true", help="Emit JSON output.")

    collect = sub.add_parser("collect", help="Execute the canonical F0n validation flow.")
    collect.add_argument("--run-id", default=default_run_id())
    collect.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    collect.add_argument("--force", action="store_true", help="Overwrite an existing run directory.")
    collect.add_argument(
        "--ae-bin",
        default="",
        help="Optional ae binary override. Defaults to `python -m ae.cli` from this repo.",
    )
    collect.add_argument("--limit-events", type=int, default=20)
    collect.add_argument(
        "--skip-egpu-passthrough-validate",
        action="store_true",
        help="Skip the guest passthrough validator. This bypasses the canonical host-A proof step.",
    )
    egpu_validate.add_target_args(collect)
    return parser.parse_args()


def _ae_prefix(ae_bin: str) -> list[str]:
    override = str(ae_bin or "").strip()
    if override:
        return [override]
    return [sys.executable, "-m", "ae.cli"]


def _ae_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = str(env.get("PYTHONPATH") or "").strip()
    parts = [str(SRC)]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_capture(*, cmd: list[str], path: Path, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    content = proc.stdout or ""
    if proc.stderr:
        if content and not content.endswith("\n"):
            content += "\n"
        content += proc.stderr
    path.write_text(content, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)} -> {path}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_collect(args: argparse.Namespace) -> int:
    plan = build_plan(run_id=str(args.run_id), runs_dir=Path(args.runs_dir))
    run_root = Path(plan["run_root"])
    if run_root.exists():
        if not args.force:
            raise SystemExit(f"run root already exists: {run_root} (use --force to overwrite)")
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(Path(plan["inventory"]["plan"]), plan)

    phases: list[dict[str, Any]] = []
    egpu_summary: dict[str, Any] | None = None
    if args.skip_egpu_passthrough_validate:
        phases.append(
            {
                "phase": "egpu_passthrough_validate",
                "status": "skipped",
                "detail": "skipped via --skip-egpu-passthrough-validate",
            }
        )
    else:
        egpu_config = egpu_validate.make_config(args)
        egpu_summary = egpu_validate.run_validation(egpu_config)
        phases.append(
            {
                "phase": "egpu_passthrough_validate",
                "status": egpu_summary["status"],
                "detail": f"guest_ip={egpu_summary['guest']['guest_ip']}",
                "checks": egpu_summary["checks"],
            }
        )
        if egpu_summary["status"] != "passed":
            summary = {
                "run_id": plan["run_id"],
                "run_root": plan["run_root"],
                "status": "failed",
                "phase_status": phases,
                "checks": egpu_summary["checks"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(Path(plan["inventory"]["summary"]), summary)
            print(json.dumps(summary, indent=2))
            return 1

    env = _ae_env()
    ae = _ae_prefix(str(args.ae_bin))
    _run_capture(cmd=[*ae, "nodes", "--json"], path=Path(plan["inventory"]["nodes"]), env=env)

    for cell in plan["cells"]:
        artifacts = cell["artifacts"]
        name = str(cell["name"])
        manifest = str(cell["manifest"])
        _run_capture(cmd=[*ae, "cell", "apply", "-f", manifest], path=Path(artifacts["apply"]), env=env)
        _run_capture(
            cmd=[*ae, "cell", "status", name, "--json"],
            path=Path(artifacts["status_initial"]),
            env=env,
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_initial"]),
            env=env,
        )
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["delete"]), env=env)
        _run_capture(
            cmd=[*ae, "cell", "apply", "-f", manifest],
            path=Path(artifacts["reapply"]),
            env=env,
        )
        _run_capture(
            cmd=[*ae, "cell", "status", name, "--json"],
            path=Path(artifacts["status_reapplied"]),
            env=env,
        )
        _run_capture(
            cmd=[*ae, "cell", "events", name, "--limit", str(args.limit_events)],
            path=Path(artifacts["events_reapplied"]),
            env=env,
        )
        _run_capture(cmd=[*ae, "cell", "delete", name], path=Path(artifacts["teardown"]), env=env)

    phases.append(
        {
            "phase": "cell_validation",
            "status": "passed",
            "detail": f"validated {len(plan['cells'])} cells",
        }
    )
    summary = {
        "run_id": plan["run_id"],
        "run_root": plan["run_root"],
        "status": "passed",
        "cell_count": len(plan["cells"]),
        "phase_status": phases,
        "checks": egpu_summary["checks"] if egpu_summary else {"egpu_passthrough_validate": "skipped"},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(Path(plan["inventory"]["summary"]), summary)
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    if args.cmd == "plan":
        plan = build_plan(run_id=str(args.run_id), runs_dir=Path(args.runs_dir))
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print(f"run_root: {plan['run_root']}")
            print(f"nodes:    {plan['inventory']['nodes']}")
            for cell in plan["cells"]:
                print(f"{cell['name']}: {cell['manifest']}")
        return 0
    if args.cmd == "collect":
        return run_collect(args)
    raise SystemExit(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
