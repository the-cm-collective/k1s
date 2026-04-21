#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_NUMERIC_KEYS = (
    "desired_replicas",
    "ready_replicas",
    "live_replicas",
    "current_revision_ready_replicas",
    "current_revision_live_replicas",
    "old_revision_ready_replicas",
    "old_revision_live_replicas",
    "overlap_ready_replicas",
    "overlap_live_replicas",
)


def _env_default(key: str, fallback: str = "") -> str:
    return str(os.getenv(key, fallback) or fallback)


def _float_default(raw: object, fallback: float = 0.0) -> float:
    try:
        return float(raw or fallback)
    except Exception:
        return fallback


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
    parser.add_argument("--duration", default=_env_default("BENCH_SNAPSHOT_DURATION", "0"))
    parser.add_argument(
        "--capture-timing",
        default=_env_default("BENCH_SNAPSHOT_CAPTURE_TIMING"),
    )
    parser.add_argument(
        "--interval",
        default=_env_default("BENCH_PHASE_TRACE_INTERVAL", "1"),
    )
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


def _status_window(samples: list[dict[str, Any]], errors: list[str], *, duration: float, interval: float) -> dict[str, Any]:
    if not samples:
        return {
            "duration_seconds": duration,
            "interval_seconds": interval,
            "sample_count": 0,
            "successful_samples": 0,
            "failed_samples": len(errors),
            "max": {},
            "last": {},
            "revision_statuses": [],
        }
    statuses = [sample["status"] for sample in samples if isinstance(sample.get("status"), dict)]
    last_status = statuses[-1] if statuses else {}
    max_status = {
        key: max(_float_default(status.get(key)) for status in statuses)
        for key in STATUS_NUMERIC_KEYS
    }
    last_metrics = {key: _float_default(last_status.get(key)) for key in STATUS_NUMERIC_KEYS}
    revision_statuses = sorted(
        {
            str(status.get("revision_status") or "").strip()
            for status in statuses
            if str(status.get("revision_status") or "").strip()
        }
    )
    return {
        "duration_seconds": duration,
        "interval_seconds": interval,
        "sample_count": len(samples) + len(errors),
        "successful_samples": len(samples),
        "failed_samples": len(errors),
        "max": max_status,
        "last": last_metrics,
        "revision_statuses": revision_statuses,
    }


def main() -> int:
    args = _parse_args()
    label = str(args.label or "").strip()
    app = str(args.app or "").strip()
    stage = str(args.stage or "").strip()
    backend = str(args.backend or "").strip()
    capture_timing = str(args.capture_timing or "").strip()
    duration = max(0.0, _float_default(args.duration))
    interval = max(0.1, _float_default(args.interval, 1.0))

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
        "capture_timing": capture_timing,
        "target_replicas": int(args.replicas) if str(args.replicas or "").isdigit() else None,
    }

    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    deadline = time.monotonic() + duration
    while True:
        captured_at = datetime.now(timezone.utc).isoformat()
        try:
            samples.append({"captured_at": captured_at, "status": _status_payload(app)})
        except Exception as exc:  # pragma: no cover - best-effort instrumentation
            errors.append(str(exc))
        if duration <= 0.0 or time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    if samples:
        payload["status"] = samples[-1]["status"]
    elif errors:
        payload["status_error"] = errors[-1]
    payload["status_window"] = _status_window(samples, errors, duration=duration, interval=interval)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    status = payload.get("status") or {}
    window = payload.get("status_window") or {}
    max_status = window.get("max") if isinstance(window, dict) else {}
    last_status = window.get("last") if isinstance(window, dict) else {}
    if isinstance(status, dict) and isinstance(max_status, dict) and isinstance(last_status, dict):
        print(
            "[phase-trace] "
            f"label={label} stage={stage or '-'} "
            f"samples={window.get('successful_samples', 0)}/{window.get('sample_count', 0)} "
            f"rev={status.get('revision')}({status.get('revision_status')}) "
            f"live={_float_default(max_status.get('live_replicas')):.0f}/{_float_default(last_status.get('live_replicas')):.0f} "
            f"cur_live={_float_default(max_status.get('current_revision_live_replicas')):.0f}/{_float_default(last_status.get('current_revision_live_replicas')):.0f} "
            f"old_live={_float_default(max_status.get('old_revision_live_replicas')):.0f}/{_float_default(last_status.get('old_revision_live_replicas')):.0f} "
            f"overlap_live={_float_default(max_status.get('overlap_live_replicas')):.0f}/{_float_default(last_status.get('overlap_live_replicas')):.0f}"
        )
    else:
        print(f"[phase-trace] label={label} stage={stage or '-'} status=unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
