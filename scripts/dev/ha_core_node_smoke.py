#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ae.controller.spec import app_key_for_manifest, load_manifest  # noqa: E402
from ae.controller.state import RegistryConflictError, state_store_from_env  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ha_core_node_smoke.py",
        description="Validate a workload-capable hub/core node in the HA VM smoke lane.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    precheck = sub.add_parser("precheck", help="Wait for the expected hub/core node to be Ready.")
    precheck.add_argument("--node-id", required=True)
    precheck.add_argument("--label", action="append", default=[])
    precheck.add_argument("--timeout", type=int, default=120)
    precheck.add_argument("--poll", type=float, default=2.0)

    smoke = sub.add_parser(
        "workload-smoke",
        help="Apply a hub-pinned smoke workload, wait for it to be Ready, then clean it up.",
    )
    smoke.add_argument("--node-id", required=True)
    smoke.add_argument("--label", action="append", default=[])
    smoke.add_argument("--manifest", type=Path, required=True)
    smoke.add_argument("--app-name", default="ha-core-node-smoke")
    smoke.add_argument("--timeout", type=int, default=180)
    smoke.add_argument("--poll", type=float, default=2.0)
    smoke.add_argument("--purge-history", action="store_true")
    return parser.parse_args()


def parse_labels(items: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in items:
        raw = str(item or "").strip()
        if not raw or "=" not in raw:
            raise SystemExit(f"invalid --label value: {item!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"invalid --label value: {item!r}")
        labels[key] = value
    return labels


def ensure_ha_env() -> None:
    os.environ.setdefault("AE_STATE_BACKEND", "etcd")


def label_mismatches(actual: dict[str, Any], expected: dict[str, str]) -> list[str]:
    mismatches: list[str] = []
    for key, value in expected.items():
        if str(actual.get(key) or "") != value:
            mismatches.append(f"{key}={value}")
    return mismatches


def find_ready_node(store, node_id: str, expected_labels: dict[str, str]) -> tuple[bool, str]:
    for node, status in store.list_nodes():
        if node.node_id != node_id:
            continue
        missing = label_mismatches(node.labels or {}, expected_labels)
        if missing:
            return False, f"node labels mismatch: {', '.join(missing)}"
        if status is None:
            return False, f"node heartbeat missing: {node_id}"
        if str(status.status or "").strip().lower() != "ready":
            return False, f"node not ready: {node_id} status={status.status}"
        return True, f"node ready: {node_id}"
    return False, f"node not found: {node_id}"


def wait_for_node_ready(store, node_id: str, expected_labels: dict[str, str], *, timeout_s: int, poll_s: float) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"node not found: {node_id}"
    while True:
        ok, detail = find_ready_node(store, node_id, expected_labels)
        if ok:
            return detail
        last_detail = detail
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def load_smoke_manifest(path: Path, app_name: str):
    manifest = load_manifest(path)
    metadata = manifest.metadata.model_copy(update={"name": app_name})
    return manifest.model_copy(update={"metadata": metadata})


def apply_manifest(store, manifest) -> int:
    app_name = app_key_for_manifest(manifest)
    existing = store.get_registered_entry(app_name)
    source = existing.source if existing else "ha-core-node-smoke"
    labels = dict(existing.labels or {}) if existing else dict(getattr(manifest.metadata, "labels", {}) or {})
    labels.setdefault("ae.harness", "ha-core-node-smoke")
    return store.register_app(
        manifest,
        source=source,
        labels=labels,
        expected_resource_version=(existing.resource_version if existing else None),
    )


def wait_for_workload_ready(store, app_name: str, *, timeout_s: int, poll_s: float) -> str:
    deadline = time.monotonic() + float(timeout_s)
    last_detail = f"workload not yet observed: {app_name}"
    while True:
        status = store.get_status(app_name)
        if status is not None:
            desired = int(status.desired_replicas or 0)
            ready = int(status.ready_replicas or 0)
            live = int(status.live_replicas or 0)
            if desired > 0 and ready >= desired and live >= desired:
                return (
                    f"workload ready: app={app_name} desired={desired} ready={ready} live={live}"
                )
            last_detail = (
                f"workload pending: app={app_name} desired={desired} ready={ready} live={live} "
                f"rev={status.revision} status={status.revision_status}"
            )
        if time.monotonic() >= deadline:
            raise SystemExit(last_detail)
        time.sleep(max(poll_s, 0.1))


def cleanup_workload(store, app_name: str, *, timeout_s: int, poll_s: float, purge_history: bool) -> None:
    existing = store.get_registered_entry(app_name)
    if existing is not None:
        try:
            store.delete_registered_app(app_name, expected_resource_version=existing.resource_version)
        except RegistryConflictError:
            refreshed = store.get_registered_entry(app_name)
            if refreshed is not None:
                store.delete_registered_app(app_name, expected_resource_version=refreshed.resource_version)

    deadline = time.monotonic() + float(timeout_s)
    while True:
        if store.get_registered_entry(app_name) is None and not store.list_pods(app_name):
            if purge_history:
                store.delete_app_state(app_name, purge_history=True)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"cleanup timeout for {app_name}")
        time.sleep(max(poll_s, 0.1))


def run_precheck(args: argparse.Namespace) -> int:
    ensure_ha_env()
    store = state_store_from_env()
    labels = parse_labels(args.label)
    detail = wait_for_node_ready(
        store,
        args.node_id,
        labels,
        timeout_s=int(args.timeout),
        poll_s=float(args.poll),
    )
    print(detail)
    return 0


def run_workload_smoke(args: argparse.Namespace) -> int:
    ensure_ha_env()
    store = state_store_from_env()
    labels = parse_labels(args.label)
    wait_for_node_ready(
        store,
        args.node_id,
        labels,
        timeout_s=int(args.timeout),
        poll_s=float(args.poll),
    )
    manifest = load_smoke_manifest(args.manifest, args.app_name)
    app_name = app_key_for_manifest(manifest)
    cleanup_error: Exception | None = None

    try:
        rv = apply_manifest(store, manifest)
        detail = wait_for_workload_ready(
            store,
            app_name,
            timeout_s=int(args.timeout),
            poll_s=float(args.poll),
        )
        print(f"workload apply rv={rv}")
        print(detail)
        return 0
    finally:
        try:
            cleanup_workload(
                store,
                app_name,
                timeout_s=max(int(args.timeout), 30),
                poll_s=float(args.poll),
                purge_history=bool(args.purge_history),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_error = exc
        if cleanup_error is not None:
            raise SystemExit(f"cleanup failed for {app_name}: {cleanup_error}") from cleanup_error


def main() -> int:
    args = parse_args()
    if args.cmd == "precheck":
        return run_precheck(args)
    if args.cmd == "workload-smoke":
        return run_workload_smoke(args)
    raise SystemExit(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
