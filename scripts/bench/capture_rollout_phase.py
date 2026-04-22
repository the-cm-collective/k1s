#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
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

CRI_NUMERIC_KEYS = (
    "pod_count",
    "current_revision_pods",
    "old_revision_pods",
    "overlap_pods",
    "main_containers",
    "current_revision_main_containers",
    "old_revision_main_containers",
    "overlap_main_containers",
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


def _run_json(argv: list[str]) -> Any:
    proc = subprocess.run(  # noqa: S603
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(argv)}")
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from {' '.join(argv)}: {exc}") from exc


def _normalize_revision(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("rev"):
        text = text[3:]
    return text


def _revision_from_value(app: str, value: str) -> str:
    if not value:
        return ""
    match = re.search(rf"{re.escape(app)}-rev([^-/]+)", value)
    if match:
        return _normalize_revision(match.group(1))
    match = re.search(r"rev([^-/]+)", value)
    if match:
        return _normalize_revision(match.group(1))
    return ""


def _revision_sort_key(value: str) -> tuple[int, int, str]:
    normalized = _normalize_revision(value)
    if normalized.isdigit():
        return (0, int(normalized), "")
    return (1, 0, normalized)


def _labels_for(item: dict[str, Any]) -> dict[str, str]:
    labels = (
        item.get("labels")
        or item.get("Labels")
        or (item.get("metadata") or {}).get("labels")
        or (item.get("Config") or {}).get("Labels")
        or (item.get("config") or {}).get("labels")
        or {}
    )
    out: dict[str, str] = {}
    for key, value in labels.items():
        out[str(key)] = str(value)
    if "ae.pod_name" not in out and "ae.replica_id" in out:
        out["ae.pod_name"] = out["ae.replica_id"]
    return out


def _item_name(item: dict[str, Any]) -> str:
    meta = item.get("metadata") or {}
    names = item.get("Names")
    if isinstance(names, list) and names:
        return str(names[0] or "")
    return str(meta.get("name") or item.get("name") or item.get("Name") or "")


def _pod_match(app: str, item: dict[str, Any]) -> tuple[bool, str]:
    labels = _labels_for(item)
    name = _item_name(item).lstrip("/")
    replica_id = labels.get("ae.pod_name", "") or labels.get("ae.replica_id", "")
    if not replica_id and name.startswith("ae-"):
        replica_id = name.removeprefix("ae-")
    matched = (
        labels.get("ae.app") == app
        or replica_id.startswith(f"{app}-rev")
        or name.startswith(f"{app}-rev")
        or name.startswith(f"ae-{app}-rev")
    )
    revision = _normalize_revision(
        labels.get("ae.revision", "").strip()
        or _revision_from_value(app, replica_id)
        or _revision_from_value(app, name)
    )
    return matched, revision


def _container_match(app: str, item: dict[str, Any]) -> tuple[bool, str, bool]:
    labels = _labels_for(item)
    name = _item_name(item).lstrip("/")
    replica_id = labels.get("ae.pod_name", "") or labels.get("ae.replica_id", "")
    if not replica_id and name.startswith("ae-"):
        replica_id = name.removeprefix("ae-")
    matched = (
        labels.get("ae.app") == app
        or replica_id.startswith(f"{app}-rev")
        or name.startswith(f"{app}-rev")
        or name.startswith(f"ae-{app}-rev")
    )
    revision = _normalize_revision(
        labels.get("ae.revision", "").strip()
        or _revision_from_value(app, replica_id)
        or _revision_from_value(app, name)
    )
    container_name = str(labels.get("ae.container") or name or "").lstrip("/")
    is_main = container_name == "main"
    return matched, revision, is_main


def _cri_payload(app: str, current_revision: str) -> dict[str, Any]:
    pods_payload = _run_json(["crictl", "pods", "-o", "json"])
    containers_payload = _run_json(["crictl", "ps", "-a", "-o", "json"])

    pod_counts: dict[str, int] = defaultdict(int)
    main_counts: dict[str, int] = defaultdict(int)
    revisions_seen: set[str] = set()

    for pod in (pods_payload.get("items") or pods_payload.get("pods") or []):
        matched, revision = _pod_match(app, pod)
        if not matched or not revision:
            continue
        pod_counts[revision] += 1
        revisions_seen.add(revision)

    for container in (containers_payload.get("containers") or containers_payload.get("items") or []):
        matched, revision, is_main = _container_match(app, container)
        if not matched or not revision or not is_main:
            continue
        main_counts[revision] += 1
        revisions_seen.add(revision)

    normalized_current = _normalize_revision(current_revision)
    if not normalized_current and revisions_seen:
        normalized_current = sorted(revisions_seen, key=_revision_sort_key)[-1]

    current_pods = pod_counts.get(normalized_current, 0) if normalized_current else 0
    old_pods = sum(
        count for revision, count in pod_counts.items() if revision != normalized_current
    )
    current_main = main_counts.get(normalized_current, 0) if normalized_current else 0
    old_main = sum(
        count for revision, count in main_counts.items() if revision != normalized_current
    )
    return {
        "current_revision": normalized_current or None,
        "revisions_seen": sorted(revisions_seen, key=_revision_sort_key),
        "pod_count": float(sum(pod_counts.values())),
        "current_revision_pods": float(current_pods),
        "old_revision_pods": float(old_pods),
        "overlap_pods": float(old_pods if current_pods > 0 and old_pods > 0 else 0),
        "main_containers": float(sum(main_counts.values())),
        "current_revision_main_containers": float(current_main),
        "old_revision_main_containers": float(old_main),
        "overlap_main_containers": float(old_main if current_main > 0 and old_main > 0 else 0),
    }


def _status_window(
    samples: list[dict[str, Any]],
    errors: list[str],
    *,
    duration: float,
    interval: float,
) -> dict[str, Any]:
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


def _cri_window(
    samples: list[dict[str, Any]],
    errors: list[str],
    *,
    duration: float,
    interval: float,
) -> dict[str, Any]:
    if not samples:
        return {
            "duration_seconds": duration,
            "interval_seconds": interval,
            "sample_count": 0,
            "successful_samples": 0,
            "failed_samples": len(errors),
            "max": {},
            "last": {},
            "revisions_seen": [],
            "current_revisions": [],
        }
    cri_samples = [sample["cri"] for sample in samples if isinstance(sample.get("cri"), dict)]
    last_sample = cri_samples[-1] if cri_samples else {}
    max_sample = {
        key: max(_float_default(sample.get(key)) for sample in cri_samples)
        for key in CRI_NUMERIC_KEYS
    }
    last_metrics = {key: _float_default(last_sample.get(key)) for key in CRI_NUMERIC_KEYS}
    revisions_seen = sorted(
        {
            str(revision).strip()
            for sample in cri_samples
            for revision in (sample.get("revisions_seen") or [])
            if str(revision).strip()
        },
        key=_revision_sort_key,
    )
    current_revisions = sorted(
        {
            _normalize_revision(sample.get("current_revision"))
            for sample in cri_samples
            if _normalize_revision(sample.get("current_revision"))
        },
        key=_revision_sort_key,
    )
    return {
        "duration_seconds": duration,
        "interval_seconds": interval,
        "sample_count": len(samples) + len(errors),
        "successful_samples": len(samples),
        "failed_samples": len(errors),
        "max": max_sample,
        "last": last_metrics,
        "revisions_seen": revisions_seen,
        "current_revisions": current_revisions,
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
    cri_samples: list[dict[str, Any]] = []
    cri_errors: list[str] = []
    deadline = time.monotonic() + duration
    last_current_revision = ""
    while True:
        captured_at = datetime.now(timezone.utc).isoformat()
        try:
            status = _status_payload(app)
            samples.append({"captured_at": captured_at, "status": status})
            last_current_revision = _normalize_revision(status.get("revision"))
        except Exception as exc:  # pragma: no cover - best-effort instrumentation
            errors.append(str(exc))
        if backend.lower() == "cri":
            try:
                cri_samples.append(
                    {
                        "captured_at": captured_at,
                        "cri": _cri_payload(app, last_current_revision),
                    }
                )
            except Exception as exc:  # pragma: no cover - best-effort instrumentation
                cri_errors.append(str(exc))
        if duration <= 0.0 or time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    if samples:
        payload["status"] = samples[-1]["status"]
    elif errors:
        payload["status_error"] = errors[-1]
    payload["status_window"] = _status_window(samples, errors, duration=duration, interval=interval)
    if cri_samples:
        payload["cri"] = cri_samples[-1]["cri"]
    elif cri_errors:
        payload["cri_error"] = cri_errors[-1]
    if backend.lower() == "cri":
        payload["cri_window"] = _cri_window(
            cri_samples,
            cri_errors,
            duration=duration,
            interval=interval,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    status = payload.get("status") or {}
    window = payload.get("status_window") or {}
    max_status = window.get("max") if isinstance(window, dict) else {}
    last_status = window.get("last") if isinstance(window, dict) else {}
    cri_window = payload.get("cri_window") or {}
    cri_max = cri_window.get("max") if isinstance(cri_window, dict) else {}
    cri_last = cri_window.get("last") if isinstance(cri_window, dict) else {}
    if isinstance(status, dict) and isinstance(max_status, dict) and isinstance(last_status, dict):
        cri_suffix = ""
        if isinstance(cri_max, dict) and isinstance(cri_last, dict) and cri_max:
            cri_suffix = (
                " "
                f"cri_cur_pods={_float_default(cri_max.get('current_revision_pods')):.0f}/{_float_default(cri_last.get('current_revision_pods')):.0f} "
                f"cri_old_pods={_float_default(cri_max.get('old_revision_pods')):.0f}/{_float_default(cri_last.get('old_revision_pods')):.0f} "
                f"cri_cur_main={_float_default(cri_max.get('current_revision_main_containers')):.0f}/{_float_default(cri_last.get('current_revision_main_containers')):.0f} "
                f"cri_old_main={_float_default(cri_max.get('old_revision_main_containers')):.0f}/{_float_default(cri_last.get('old_revision_main_containers')):.0f}"
            )
        print(
            "[phase-trace] "
            f"label={label} stage={stage or '-'} "
            f"samples={window.get('successful_samples', 0)}/{window.get('sample_count', 0)} "
            f"rev={status.get('revision')}({status.get('revision_status')}) "
            f"live={_float_default(max_status.get('live_replicas')):.0f}/{_float_default(last_status.get('live_replicas')):.0f} "
            f"cur_live={_float_default(max_status.get('current_revision_live_replicas')):.0f}/{_float_default(last_status.get('current_revision_live_replicas')):.0f} "
            f"old_live={_float_default(max_status.get('old_revision_live_replicas')):.0f}/{_float_default(last_status.get('old_revision_live_replicas')):.0f} "
            f"overlap_live={_float_default(max_status.get('overlap_live_replicas')):.0f}/{_float_default(last_status.get('overlap_live_replicas')):.0f}"
            f"{cri_suffix}"
        )
    else:
        print(f"[phase-trace] label={label} stage={stage or '-'} status=unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
