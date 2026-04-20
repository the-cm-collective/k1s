#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SteadySample:
    revisions: tuple[str, ...]
    app_present: bool
    live_container_count: int
    orphan_count: int
    detail: str

    @property
    def is_steady(self) -> bool:
        return self.app_present and len(self.revisions) == 1 and self.orphan_count == 0


def _run_json(argv: list[str]) -> Any:
    proc = subprocess.run(  # noqa: S603
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"command failed: {' '.join(argv)}")
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from {' '.join(argv)}: {exc}") from exc


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


def _revision_from_value(app: str, value: str) -> str:
    if not value:
        return ""
    match = re.search(rf"{re.escape(app)}-(rev[^-/]+)", value)
    if match:
        return match.group(1)
    match = re.search(r"(rev[^-/]+)", value)
    if match:
        return match.group(1)
    return ""


def _container_match(
    app: str,
    item: dict[str, Any],
    labels: dict[str, str],
) -> tuple[bool, str]:
    name = _item_name(item).lstrip("/")
    replica_id = labels.get("ae.pod_name", "") or labels.get("ae.replica_id", "")
    if not replica_id and name.startswith("ae-"):
        replica_id = name.removeprefix("ae-")
    matched = (
        labels.get("ae.app") == app
        or replica_id.startswith(f"{app}-rev")
        or name.startswith(f"ae-{app}-rev")
        or name.startswith(f"{app}-rev")
    )
    revision = (
        labels.get("ae.revision", "").strip()
        or _revision_from_value(app, replica_id)
        or _revision_from_value(app, name)
    )
    return matched, revision


def parse_cri_sample(app: str, pods_payload: Any, containers_payload: Any) -> SteadySample:
    pods = (pods_payload or {}).get("items") or (pods_payload or {}).get("pods") or []
    containers = (
        (containers_payload or {}).get("containers")
        or (containers_payload or {}).get("items")
        or []
    )

    pod_ids: set[str] = set()
    live_pod_ids: set[str] = set()
    revisions: set[str] = set()

    for pod in pods:
        labels = _labels_for(pod)
        name = _item_name(pod)
        pod_id = str(pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id") or "")
        if pod_id:
            live_pod_ids.add(pod_id)
        if labels.get("ae.app") == app or name.startswith(f"{app}-rev"):
            if pod_id:
                pod_ids.add(pod_id)
            revision = labels.get("ae.revision", "").strip() or _revision_from_value(app, name)
            if revision:
                revisions.add(revision)

    orphan_ids: set[str] = set()
    live_container_count = 0
    for container in containers:
        labels = _labels_for(container)
        matched, revision = _container_match(app, container, labels)
        if not matched:
            continue
        live_container_count += 1
        if revision:
            revisions.add(revision)
        pod_id = str(
            container.get("podSandboxId")
            or container.get("pod_sandbox_id")
            or container.get("pod_id")
            or ""
        )
        if pod_id:
            pod_ids.add(pod_id)
        if pod_id and pod_id in live_pod_ids:
            continue
        cid = str(
            container.get("id")
            or container.get("containerId")
            or container.get("container_id")
            or ""
        )
        if cid:
            orphan_ids.add(cid)

    app_present = bool(pod_ids or live_container_count)
    detail = (
        f"revisions={','.join(sorted(revisions)) or '-'} "
        f"containers={live_container_count} "
        f"orphans={len(orphan_ids)}"
    )
    return SteadySample(
        revisions=tuple(sorted(revisions)),
        app_present=app_present,
        live_container_count=live_container_count,
        orphan_count=len(orphan_ids),
        detail=detail,
    )


def parse_container_sample(app: str, containers: list[dict[str, Any]]) -> SteadySample:
    revisions: set[str] = set()
    app_present = False
    live_container_count = 0
    orphan_count = 0

    for container in containers:
        labels = _labels_for(container)
        matched, revision = _container_match(app, container, labels)
        if not matched:
            continue
        app_present = True
        live_container_count += 1
        if revision:
            revisions.add(revision)
        state = container.get("State")
        if isinstance(state, dict):
            status = str(state.get("Status") or "").strip().lower()
        else:
            status = (
                str(state or container.get("status") or container.get("Status") or "")
                .strip()
                .lower()
            )
        if status and status not in {"running", "created", "up"}:
            orphan_count += 1
        if not revision:
            orphan_count += 1

    detail = (
        f"revisions={','.join(sorted(revisions)) or '-'} "
        f"containers={live_container_count} "
        f"orphans={orphan_count}"
    )
    return SteadySample(
        revisions=tuple(sorted(revisions)),
        app_present=app_present,
        live_container_count=live_container_count,
        orphan_count=orphan_count,
        detail=detail,
    )


def collect_cri_sample(app: str) -> SteadySample:
    endpoint = os.getenv("AE_CRI_ENDPOINT", "unix:///run/containerd/containerd.sock")
    pods_payload = _run_json(["crictl", "--runtime-endpoint", endpoint, "pods", "-o", "json"])
    containers_payload = _run_json(
        ["crictl", "--runtime-endpoint", endpoint, "ps", "-a", "-o", "json"]
    )
    return parse_cri_sample(app, pods_payload, containers_payload)


def collect_podman_sample(app: str) -> SteadySample:
    podman_bin = os.getenv("AE_PODMAN_BIN", "podman")
    payload = _run_json([podman_bin, "ps", "-a", "--format", "json"])
    containers = payload if isinstance(payload, list) else []
    return parse_container_sample(app, containers)


def collect_docker_sample(app: str) -> SteadySample:
    ids_proc = subprocess.run(  # noqa: S603,S607
        ["docker", "ps", "-aq"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if ids_proc.returncode != 0:
        raise RuntimeError(ids_proc.stderr.strip() or "docker ps failed")
    ids = [item.strip() for item in (ids_proc.stdout or "").splitlines() if item.strip()]
    if not ids:
        return parse_container_sample(app, [])
    payload = _run_json(["docker", "inspect", *ids])
    containers = payload if isinstance(payload, list) else []
    return parse_container_sample(app, containers)


def collect_sample(backend: str, app: str) -> SteadySample:
    if backend == "cri":
        return collect_cri_sample(app)
    if backend == "podman":
        return collect_podman_sample(app)
    if backend == "docker":
        return collect_docker_sample(app)
    raise ValueError(f"unsupported backend: {backend}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Wait until an app is in a single-revision steady state for the selected backend."
        )
    )
    parser.add_argument("--backend", required=True, choices=("cri", "podman", "docker"))
    parser.add_argument("--app", required=True)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--delay", type=int, default=2)
    parser.add_argument("--stable-polls", type=int, default=3)
    parser.add_argument("--require-app-present", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    deadline = time.monotonic() + max(args.timeout, 1)
    stable_hits = 0
    last_detail = "no sample collected"

    while True:
        sample = collect_sample(args.backend, args.app)
        last_detail = sample.detail
        ready = sample.is_steady
        if args.require_app_present and not sample.app_present:
            ready = False
        if ready:
            stable_hits += 1
            if stable_hits >= max(args.stable_polls, 1):
                print(
                    f"[steady] backend={args.backend} app={args.app} ok "
                    f"{sample.detail} polls={stable_hits}"
                )
                return 0
        else:
            stable_hits = 0
        if time.monotonic() >= deadline:
            print(
                f"[steady] backend={args.backend} app={args.app} timeout {last_detail}",
                file=sys.stderr,
            )
            return 1
        time.sleep(max(args.delay, 1))


if __name__ == "__main__":
    raise SystemExit(main())
