#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env_default(key: str, fallback: str = "") -> str:
    return str(os.getenv(key, fallback) or fallback)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture rollout phase state for a snapshot label using ae status --json."
        )
    )
    parser.add_argument("--app", default=_env_default("BENCH_APP_NAME"))
    parser.add_argument("--backend", default=_env_default("BENCH_BACKEND"))
    parser.add_argument("--label", default=_env_default("BENCH_SNAPSHOT_LABEL"))
    parser.add_argument("--stage", default=_env_default("BENCH_SNAPSHOT_STAGE"))
    parser.add_argument("--replicas", default=_env_default("BENCH_SNAPSHOT_REPLICAS"))
    parser.add_argument(
        "--output",
        default="",
        help="Optional explicit output path. Defaults to snapshots/<label>/phase-trace.json.",
    )
    return parser.parse_args()


def _status_payload(app: str) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ae.cli", "status", app, "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "ae status failed")
    payload = json.loads(proc.stdout or "{}")
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"unexpected ae status payload type: {type(payload).__name__}")


def main() -> int:
    args = _parse_args()
    label = str(args.label or "").strip()
    app = str(args.app or "").strip()
    stage = str(args.stage or "").strip()
    backend = str(args.backend or "").strip()

    if not label:
        print("[phase-trace] missing snapshot label", file=sys.stderr)
        return 2
    if not app:
        print("[phase-trace] missing app name", file=sys.stderr)
        return 2

    output = Path(args.output) if args.output else Path("snapshots") / label / "phase-trace.json"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "stage": stage,
        "backend": backend,
        "app": app,
        "target_replicas": int(args.replicas) if str(args.replicas or "").isdigit() else None,
    }

    try:
        payload["status"] = _status_payload(app)
    except Exception as exc:  # pragma: no cover - best-effort instrumentation
        payload["status_error"] = str(exc)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    status = payload.get("status") or {}
    if isinstance(status, dict):
        print(
            "[phase-trace] "
            f"label={label} stage={stage or '-'} "
            f"rev={status.get('revision')}({status.get('revision_status')}) "
            f"live={status.get('live_replicas')} "
            f"cur_live={status.get('current_revision_live_replicas')} "
            f"old_live={status.get('old_revision_live_replicas')} "
            f"overlap_live={status.get('overlap_live_replicas')}"
        )
    else:
        print(f"[phase-trace] label={label} stage={stage or '-'} status=unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
