#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _labels_for(item: dict[str, object]) -> dict[str, str]:
    labels = item.get("Labels")
    if isinstance(labels, dict) and labels:
        return {str(k): str(v) for k, v in labels.items()}
    config = item.get("Config")
    if isinstance(config, dict):
        cfg_labels = config.get("Labels")
        if isinstance(cfg_labels, dict) and cfg_labels:
            return {str(k): str(v) for k, v in cfg_labels.items()}
    return {}


def _container_matches_app(labels: dict[str, str], app_name: str) -> bool:
    replica_id = labels.get("ae.pod_name") or labels.get("ae.replica_id") or ""
    return labels.get("ae.app") == app_name or replica_id.startswith(f"{app_name}-rev")


def check_snapshot(snapshot_dir: Path, app_name: str, max_app_bytes: int = 0) -> list[str]:
    errors: list[str] = []
    summary_path = snapshot_dir / "summary.json"
    if not summary_path.exists():
        return [f"missing summary.json in {snapshot_dir}"]

    summary = _load_json(summary_path)
    if not isinstance(summary, dict):
        return [f"invalid summary.json in {snapshot_dir}"]

    containers = summary.get("containers")
    app_bytes = 0
    if isinstance(containers, dict):
      app_bytes = int(containers.get("app_mem_bytes") or 0)
    if app_bytes > max_app_bytes:
        errors.append(
            f"idle app_mem_bytes={app_bytes} exceeds max_app_bytes={max_app_bytes}"
        )

    offenders: list[str] = []
    raw_dir = snapshot_dir / "raw"
    for inspect_name in ("cri_inspect.json", "podman_inspect.json", "docker_inspect.json"):
        inspect_path = raw_dir / inspect_name
        if not inspect_path.exists():
            continue
        payload = _load_json(inspect_path)
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            labels = _labels_for(item)
            if not _container_matches_app(labels, app_name):
                continue
            cid = str(item.get("Id") or item.get("id") or "")[:12]
            name = str(item.get("Name") or item.get("name") or "")
            offenders.append(f"{inspect_name}:{cid}:{name}")

    if offenders:
        preview = ", ".join(offenders[:5])
        errors.append(f"idle snapshot still captured app-owned containers: {preview}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot_dir", type=Path)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--max-app-bytes", type=int, default=0)
    args = parser.parse_args()

    errors = check_snapshot(args.snapshot_dir, args.app_name, args.max_app_bytes)
    if errors:
        for error in errors:
            print(f"[check-idle-snapshot] {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
