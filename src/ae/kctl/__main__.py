"""kubectl-like CLI for working with the ae/k1s engine.

This provides familiar commands (`get`, `describe`, `apply`, `rollout`, `logs`, `events`)
that map onto the existing `ae.cli` functionality. k1s Apps are Deployment-like workloads.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple

from ae.cli.__main__ import (
    format_status,
    handle_logs,
    handle_metrics,
    handle_registry,
    registry_auth_factory,
    runtime_factory,
    health_manager_factory,
    ingress_service_factory,
    secret_manager_factory,
)
from ae.controller.spec import app_key, format_app_ref, parse_app_ref
from ae.observability.logging import configure_logging
from ae.controller.reconciler import Reconciler
from ae.controller.state import RegistryConflictError, SQLiteStateStore, state_store_from_env
from ae.ingress.service import IngressService
from ae.kctl.lifecycle import LIFECYCLE_ACTIONS, PROFILE_NAMES, LifecycleManager, LifecycleResult
from ae.kctl.profile_backend import (
    PORTABLE_PROFILES,
    PortableLifecycleResult,
    PortableProfileManager,
)
from ae.kctl.profile_bootstrap import ProfileBootstrapStore
from ae.kctl.profile_engine import SelectedEngineProbe
from ae.kctl.profile_host_plan import (
    ProfileHostPlanError,
    plan_strict_cri_from_files,
    render_strict_cri_host_plan_json,
    render_strict_cri_host_plan_text,
)
from ae.kctl.profile_host_transaction import (
    PipeHostHelperClient,
    ProfileHostTransactionError,
    StrictCRIHostTransactionManager,
)
from ae.kctl.profile_migration import (
    MIGRATION_PROFILES,
    ProfileMigrationManager,
    render_migration_text,
)
from ae.kctl.profile_paths import ProfilePathError, ProfileRoots
from ae.kctl.profile_node_adapter import StrictCRINodeInputs
from ae.kctl.profile_state import ProfileStateError
from ae.kctl.profiles import (
    PROFILE_INPUT_NAMES,
    ReadOnlyDoctorProbe,
    SystemDoctorProbe,
    profile_catalog_payload,
    profile_doctor_payload,
    profile_plan_payload,
    render_profile_text,
)
from ae.controller.__main__ import service_controller_factory


# ----------------------------
# Parsing helpers
# ----------------------------


@dataclass(slots=True)
class ParsedRef:
    kind: str
    name: str


def _resolve_app_name(raw: str) -> str:
    ns, base = parse_app_ref(raw)
    return app_key(base, ns)


def _display_app_name(app_name: str) -> str:
    return format_app_ref(app_name)


def _ha_mode_enabled() -> bool:
    return str(os.getenv("AE_HA_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}


def parse_ref(arg: str, expected: tuple[str, ...]) -> ParsedRef:
    """Parse resource reference like "app/echo" or provide NAME + kind default.

    - Accepts forms:
      * "app/NAME" or "apps/NAME"
      * "deploy/NAME", "deployment/NAME", or "deployments/NAME"
      * "workload/NAME" or "workloads/NAME"
      * just NAME (defaults to the first `expected` kind)
    - Kinds are normalized to singular: "app".
    """

    name = arg
    if "/" in arg:
        raw_kind, maybe_name = arg.split("/", 1)
        kind = raw_kind.lower()
        if kind in {"app", "apps", "deployment", "deployments", "deploy", "workload", "workloads"}:
            name = maybe_name
        else:
            kind = expected[0]
            name = arg
    else:
        kind = expected[0]

    app_aliases = {
        "app",
        "apps",
        "deployment",
        "deployments",
        "deploy",
        "workload",
        "workloads",
    }
    kind_norm = "app" if kind in app_aliases else kind
    if kind_norm not in expected:
        raise argparse.ArgumentTypeError(
            f"Unsupported resource kind '{kind}'. Expected one of: {', '.join(expected)}"
        )
    if kind_norm == "app":
        name = _resolve_app_name(name)
    return ParsedRef(kind=kind_norm, name=name)


# ----------------------------
# CLI construction
# ----------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="k1s", description="kubectl-like CLI for k1s (Apps are Deployment-like)"
    )
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    p.add_argument(
        "--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # k1s get apps|app|pods|services <name?>
    get_p = sub.add_parser("get", help="List resources or show a resource")
    get_p.add_argument(
        "resource",
        choices=[
            "apps",
            "app",
            "deployments",
            "deployment",
            "workloads",
            "workload",
            "deploy",
            "pods",
            "pod",
            "services",
            "service",
        ],
        help="Resource type",
    )
    get_p.add_argument("name", nargs="?", help="Optional resource name")
    get_p.add_argument("-o", "--output", choices=["wide", "json"], default="wide")

    # k1s describe app/<name>
    desc_p = sub.add_parser("describe", help="Describe a resource")
    desc_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")

    # k1s apply -f <file>
    apply_p = sub.add_parser("apply", help="Apply a manifest file")
    apply_p.add_argument("-f", "--file", type=Path, required=True)

    # k1s rollout history app/<name> | k1s rollout undo app/<name> [--to-revision N]
    roll_p = sub.add_parser("rollout", help="Manage rollouts")
    roll_sub = roll_p.add_subparsers(dest="roll_cmd", required=True)

    hist_p = roll_sub.add_parser("history", help="Show rollout history for a resource")
    hist_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    hist_p.add_argument("--limit", type=int, default=10)

    undo_p = roll_sub.add_parser("undo", help="Rollback/undo to a previous revision")
    undo_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    undo_p.add_argument("--to-revision", type=int, default=None)

    # k1s logs app/<name> [-f]
    logs_p = sub.add_parser("logs", help="Show workload logs")
    logs_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    logs_p.add_argument("-f", "--follow", action="store_true")
    logs_p.add_argument("--container", default=None, help="Replica selector: index or id")
    logs_p.add_argument("--revision", type=int, default=None, help="Filter by revision")
    logs_p.add_argument("--tail", type=int, default=None, help="Limit output lines")
    logs_p.add_argument("--since", default=None, help="Relative time window: 5m, 1h")
    logs_p.add_argument("--since-time", dest="since_time", default=None, help="RFC3339 timestamp")

    # bonus passthroughs
    events_p = sub.add_parser("events", help="Show recent events for a workload/app")
    events_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    events_p.add_argument("--limit", type=int, default=20)

    # k1s delete app/<name>
    del_p = sub.add_parser("delete", help="Delete a workload/app (containers + status)")
    del_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    del_p.add_argument("--purge", action="store_true", help="Also purge events and revisions")

    # k1s scale app/<name> --replicas N
    sc_p = sub.add_parser("scale", help="Scale a workload/app by reconciling replicas")
    sc_p.add_argument("ref", help="Resource ref: app/NAME, deployment/NAME, or NAME")
    sc_p.add_argument("--replicas", type=int, required=True)

    # Installed, non-mutating profile inspection plus legacy lifecycle actions.
    profile_p = sub.add_parser("profile", help="Manage a supported k1s runtime profile")
    profile_sub = profile_p.add_subparsers(dest="profile_action", required=True)
    list_p = profile_sub.add_parser("list", help="List installed profile contracts")
    list_p.add_argument("-o", "--output", choices=["text", "json"], default="text")
    for action in ("doctor", "plan"):
        action_p = profile_sub.add_parser(
            action,
            help=f"{action.capitalize()} a profile without mutating the host",
        )
        action_p.add_argument("profile", choices=PROFILE_INPUT_NAMES)
        action_p.add_argument("-o", "--output", choices=["text", "json"], default="text")
        if action == "doctor":
            action_p.add_argument(
                "--engine",
                choices=("podman", "docker"),
                default=None,
                help="Explicit bounded read-only engine capability query",
            )
    lifecycle_names = tuple(dict.fromkeys((*PORTABLE_PROFILES, *PROFILE_NAMES)))
    for action in LIFECYCLE_ACTIONS:
        action_p = profile_sub.add_parser(action, help=f"{action.capitalize()} a runtime profile")
        action_p.add_argument("profile", choices=lifecycle_names)
        action_p.add_argument("-o", "--output", choices=["text", "json"], default="text")
        action_p.add_argument(
            "--legacy",
            action="store_true",
            help="Use the repository-local compatibility runner where supported",
        )
        if action in {"start", "restart"}:
            action_p.add_argument("--backend", choices=("podman", "docker"), default=None)
            action_p.add_argument(
                "--bootstrap-id",
                default=None,
                help="Exact owner-private bootstrap identity for edge-gateway or node",
            )
            for option in (
                "host-plan",
                "expect-host-plan-sha256",
                "expect-host-plan-digest",
                "host-receipt",
                "expect-host-receipt-sha256",
            ):
                action_p.add_argument(f"--{option}", default=None)

    run_p = profile_sub.add_parser("run", help="Run a portable profile in the foreground")
    run_p.add_argument("profile", choices=PORTABLE_PROFILES)
    run_p.add_argument("--backend", choices=("podman", "docker"), default=None)
    run_p.add_argument(
        "--bootstrap-id",
        default=None,
        help="Exact owner-private bootstrap identity for edge-gateway or node",
    )
    for option in (
        "host-plan",
        "expect-host-plan-sha256",
        "expect-host-plan-digest",
        "host-receipt",
        "expect-host-receipt-sha256",
    ):
        run_p.add_argument(f"--{option}", default=None)
    run_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    logs_profile_p = profile_sub.add_parser("logs", help="Read an exact owned component log")
    logs_profile_p.add_argument("profile", choices=PORTABLE_PROFILES)
    logs_profile_p.add_argument("--component", required=True)
    logs_profile_p.add_argument("--run-id", default=None)
    logs_profile_p.add_argument("--tail", type=int, default=200)
    logs_profile_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    delete_profile_p = profile_sub.add_parser(
        "delete", help="Delete one exact cleaned stopped profile run"
    )
    delete_profile_p.add_argument("profile", choices=PORTABLE_PROFILES)
    delete_profile_p.add_argument("--run-id", required=True)
    delete_profile_p.add_argument("--confirm-delete", required=True)
    delete_profile_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    bootstrap_p = profile_sub.add_parser(
        "bootstrap",
        help="Create one immutable owner-private canonical role bootstrap",
    )
    bootstrap_sub = bootstrap_p.add_subparsers(dest="bootstrap_action", required=True)
    create_p = bootstrap_sub.add_parser("create", help="Create one exact bootstrap record")
    create_sub = create_p.add_subparsers(dest="bootstrap_profile", required=True)

    gateway_p = create_sub.add_parser("edge-gateway", help="Create a gateway-only bootstrap")
    gateway_p.add_argument("--site-id", required=True)
    gateway_p.add_argument("--node-id", required=True)
    gateway_p.add_argument("--transport-url", required=True)
    gateway_p.add_argument("--credential-file", type=Path, required=True)
    gateway_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    node_p = create_sub.add_parser("node", help="Create an identity-v1 node bootstrap")
    node_p.add_argument("--node-id", required=True)
    node_p.add_argument("--controller-url", required=True)
    node_p.add_argument(
        "--runtime-backend", choices=("podman", "docker", "strict-cri"), required=True
    )
    node_p.add_argument("--listen-host", required=True)
    node_p.add_argument("--listen-port", type=int, required=True)
    node_p.add_argument("--advertise-endpoint", required=True)
    node_p.add_argument("--credential-file", type=Path, required=True)
    node_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    migrate_p = profile_sub.add_parser(
        "migrate",
        help="Plan or transact one exact stopped legacy profile migration",
    )
    migrate_sub = migrate_p.add_subparsers(dest="migration_action", required=True)
    for migration_action in ("plan", "apply", "rollback"):
        action_p = migrate_sub.add_parser(
            migration_action,
            help=f"{migration_action.capitalize()} an explicit legacy migration",
        )
        action_p.add_argument("legacy_profile", choices=MIGRATION_PROFILES)
        action_p.add_argument("--legacy-root", type=Path, required=True)
        action_p.add_argument("--destination-profile", required=True)
        action_p.add_argument("--destination-root", type=Path, required=True)
        action_p.add_argument("-o", "--output", choices=("text", "json"), default="text")
        if migration_action == "apply":
            action_p.add_argument("--expect-plan-digest", required=True)
        elif migration_action == "rollback":
            action_p.add_argument("--migration-id", required=True)
            action_p.add_argument("--expect-receipt-digest", required=True)
            action_p.add_argument("--confirm-rollback", required=True)

    host_p = profile_sub.add_parser(
        "host",
        help="Plan an explicit host contract without probing or mutating the host",
    )
    host_sub = host_p.add_subparsers(dest="host_action", required=True)
    host_plan_p = host_sub.add_parser("plan", help="Create one pure host plan")
    host_plan_sub = host_plan_p.add_subparsers(dest="host_profile", required=True)
    strict_cri_p = host_plan_sub.add_parser(
        "strict-cri",
        help="Plan explicit Linux containerd CRI-v1 preparation",
    )
    strict_cri_p.add_argument("--intent", required=True)
    strict_cri_p.add_argument("--expect-intent-sha256", required=True)
    strict_cri_p.add_argument("--facts", required=True)
    strict_cri_p.add_argument("--expect-facts-sha256", required=True)
    strict_cri_p.add_argument("-o", "--output", choices=("text", "json"), default="text")

    for host_action in ("prepare", "rollback"):
        action_p = host_sub.add_parser(
            host_action,
            help=f"{host_action.capitalize()} one digest-bound strict-CRI host transaction",
        )
        action_sub = action_p.add_subparsers(dest="host_profile", required=True)
        strict_p = action_sub.add_parser("strict-cri")
        strict_p.add_argument("--transaction-root", type=Path, required=True)
        strict_p.add_argument("--transaction-id", required=True)
        strict_p.add_argument("--helper-request-fd", type=int, required=True)
        strict_p.add_argument("--helper-response-fd", type=int, required=True)
        strict_p.add_argument("--expect-helper-identity-sha256", required=True)
        strict_p.add_argument("-o", "--output", choices=("text", "json"), default="text")
        if host_action == "prepare":
            strict_p.add_argument("--intent", type=Path, required=True)
            strict_p.add_argument("--expect-intent-sha256", required=True)
            strict_p.add_argument("--facts", type=Path, required=True)
            strict_p.add_argument("--expect-facts-sha256", required=True)
            strict_p.add_argument("--plan", type=Path, required=True)
            strict_p.add_argument("--expect-plan-sha256", required=True)
            strict_p.add_argument("--expect-plan-digest", required=True)
            strict_p.add_argument("--confirm-prepare", required=True)
        else:
            strict_p.add_argument("--expect-applied-receipt-sha256", required=True)
            strict_p.add_argument("--confirm-rollback", required=True)

    return p


# ----------------------------
# Command handlers
# ----------------------------


def _setup() -> tuple[SQLiteStateStore, Reconciler, object]:
    store = state_store_from_env()
    registry_auth = registry_auth_factory()
    runtime = runtime_factory(registry_auth=registry_auth)
    health = health_manager_factory()
    ingress = ingress_service_factory()
    secrets = secret_manager_factory()
    svc_controller = service_controller_factory(store)
    reconciler = Reconciler(
        runtime,
        store,
        health_manager=health,
        ingress_service=ingress,
        secret_manager=secrets,
        service_controller=svc_controller,
    )
    return store, reconciler, runtime


def handle_get(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    def _print_json(payload: object) -> None:
        import json

        print(json.dumps(payload, indent=2))

    def _collect_pods(app_filter: str | None = None) -> list[dict]:
        pods: list[dict] = []
        statuses = store.list_status()
        for st in statuses:
            if app_filter and st.app_name != app_filter:
                continue
            rows = store.list_pod_nodes(st.app_name)
            for row in rows:
                pod_name, node_id, ready, live, status, readiness, liveness = row
                pods.append(
                    {
                        "app": st.app_name,
                        "pod_name": pod_name,
                        "replica_id": pod_name,
                        "ready": bool(ready),
                        "live": bool(live),
                        "status": status,
                        "node": node_id,
                        "readiness_message": readiness,
                        "liveness_message": liveness,
                    }
                )
        return pods

    def _print_pods(pods: list[dict]) -> None:
        if ns.output == "json":
            _print_json(pods)
            return
        for pod in pods:
            node = pod.get("node") or "-"
            print(
                f"{pod.get('replica_id')}: ready={pod.get('ready')} live={pod.get('live')} "
                f"status={pod.get('status')} node={node} app={_display_app_name(pod.get('app'))}"
            )

    def _print_services(services: list[dict]) -> None:
        if ns.output == "json":
            _print_json(services)
            return
        for svc in services:
            print(f"{_display_app_name(svc.get('app'))}: cluster_ip={svc.get('cluster_ip')}")

    if ns.resource in {"pods", "pod"}:
        app_filter = None
        replica_filter = None
        if ns.name:
            raw = ns.name
            if "/" in raw:
                kind, val = raw.split("/", 1)
                if kind in {
                    "app",
                    "apps",
                    "deployment",
                    "deployments",
                    "deploy",
                    "workload",
                    "workloads",
                }:
                    app_filter = _resolve_app_name(val)
                else:
                    replica_filter = raw
            else:
                replica_filter = raw
        pods = _collect_pods(app_filter=app_filter)
        if replica_filter and not app_filter:
            matches = [p for p in pods if p.get("replica_id") == replica_filter]
            if matches:
                _print_pods(matches)
                return 0
            resolved = _resolve_app_name(replica_filter)
            if store.get_status(resolved) is not None:
                pods = _collect_pods(app_filter=resolved)
            else:
                print(f"No pod/replica recorded for {replica_filter}")
                return 1
        if not pods:
            print("No pods recorded.")
            return 0
        _print_pods(pods)
        return 0

    if ns.resource in {"services", "service"}:
        if ns.name:
            app = _resolve_app_name(ns.name)
            rec = store.get_service(app)
            if rec is None:
                print(f"No service recorded for {_display_app_name(app)}")
                return 1
            endpoints = store.list_service_endpoints(app)
            if ns.output == "json":
                _print_json(
                    {
                        "app": rec.app_name,
                        "cluster_ip": rec.cluster_ip,
                        "ports": rec.ports,
                        "endpoints": [
                            {
                                "port": ep.port,
                                "ip": ep.ip,
                                "target_port": ep.target_port,
                                "ready": ep.ready,
                            }
                            for ep in endpoints
                        ],
                    }
                )
                return 0
            print(f"{_display_app_name(rec.app_name)}: cluster_ip={rec.cluster_ip}")
            if rec.ports:
                print(f"  ports: {rec.ports}")
            for ep in endpoints:
                print(f"  - port={ep.port} ip={ep.ip} target={ep.target_port} ready={ep.ready}")
            return 0
        services = store.list_services()
        if not services:
            print("No services recorded.")
            return 0
        payload = [{"app": s.app_name, "cluster_ip": s.cluster_ip} for s in services]
        _print_services(payload)
        return 0

    if ns.name:
        ref = parse_ref(f"{ns.resource}/{ns.name}", ("app",))
        status = store.get_status(ref.name)
        if status is None:
            print(f"No status recorded for {_display_app_name(ref.name)}")
            return 1
        print(format_status(status))
        return 0

    statuses = store.list_status()
    if not statuses:
        print("No workloads recorded.")
        return 0
    for s in statuses:
        print(format_status(s))
    return 0


def handle_describe(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    ref = parse_ref(ns.ref, ("app",))
    status = store.get_status(ref.name)
    if status is None:
        print(f"No status recorded for {_display_app_name(ref.name)}")
        return 1
    print(format_status(status))

    pods = store.list_pods(ref.name)
    for r in pods:
        print(
            f"  - {r.pod_name}: ready={r.ready} live={r.live} status={r.status} | "
            f"readiness={r.readiness_message}; liveness={r.liveness_message}"
        )
    events = store.list_events(ref.name, limit=10)
    if not events:
        print("    no events recorded")
    else:
        for e in events:
            ts = e.created_at.strftime("%Y-%m-%d %H:%M:%S")
            print(f"    event {ts} rev={e.revision} {e.event_type}: {e.message}")
    return 0


def handle_apply(ns: argparse.Namespace, reconciler: Reconciler) -> int:
    if _ha_mode_enabled():
        from ae.controller.spec import load_manifest

        manifest = load_manifest(Path(ns.file))
        store = state_store_from_env()
        existing = store.get_registered_entry(
            app_key(manifest.metadata.name, manifest.metadata.namespace)
        )
        src = existing.source if existing else "kctl"
        lbls = existing.labels if existing else getattr(manifest.metadata, "labels", None)
        try:
            rv = store.register_app(
                manifest,
                source=src,
                labels=lbls,
                expected_resource_version=(existing.resource_version if existing else None),
            )
        except RegistryConflictError as exc:
            print(f"apply conflict: {exc}")
            return 1
        print(
            f"applied desired state for {_display_app_name(app_key(manifest.metadata.name, manifest.metadata.namespace))} "
            f"resourceVersion={rv}"
        )
        return 0
    report = reconciler.reconcile_manifest_path(ns.file)
    print(
        f"applied {_display_app_name(report.app_name)} rev={report.revision}({report.revision_status}) "
        f"ops=+{report.created}/~{report.updated}/-{report.removed} "
        f"ready={report.ready_replicas} live={report.live_replicas}"
    )
    return 0


def handle_rollout(ns: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler) -> int:
    if ns.roll_cmd == "history":
        ref = parse_ref(ns.ref, ("app",))
        revs = store.list_revisions(ref.name, limit=ns.limit)
        if not revs:
            print(f"No revisions recorded for {_display_app_name(ref.name)}.")
            return 0
        for info in revs:
            print(
                f"rev {info.revision}: status={info.status}, image={info.image}, "
                f"hash={info.spec_hash[:8]}"
            )
        return 0

    if ns.roll_cmd == "undo":
        ref = parse_ref(ns.ref, ("app",))
        target = ns.__dict__.get("to_revision")
        if target is None:
            revs = store.list_revisions(ref.name, limit=2)
            if len(revs) < 2:
                print("No previous revision to roll back to.")
                return 1
            target = revs[1].revision
        try:
            manifest = store.get_revision_manifest(ref.name, target)
        except ValueError as exc:  # invalid revision
            print(str(exc))
            return 1
        report = reconciler.reconcile(manifest)
        print(
            f"rolled back {_display_app_name(ref.name)} to revision {report.revision} ({report.revision_status})"
        )
        return 0

    print(f"Unsupported rollout command: {ns.roll_cmd}")
    return 1


def handle_logs_k1s(ns: argparse.Namespace, store: SQLiteStateStore, runtime) -> int:
    ref = parse_ref(ns.ref, ("app",))

    class _Args(argparse.Namespace):
        pass

    adapted = _Args()
    adapted.name = ref.name
    adapted.follow = ns.follow
    adapted.revision = ns.revision
    adapted.container = ns.container
    adapted.tail = ns.tail
    adapted.since = ns.since
    adapted.since_time = ns.since_time
    return handle_logs(adapted, store, runtime)


def handle_events_k1s(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    ref = parse_ref(ns.ref, ("app",))
    events = store.list_events(ref.name, limit=ns.limit)
    if not events:
        print(f"No events recorded for {_display_app_name(ref.name)}.")
        return 0
    for e in events:
        ts = e.created_at.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} rev={e.revision} {e.event_type}: {e.message}")
    return 0


def handle_delete_k1s(
    ns: argparse.Namespace,
    store: SQLiteStateStore,
    reconciler: Reconciler,
    runtime,
) -> int:
    ref = parse_ref(ns.ref, ("app",))
    if _ha_mode_enabled():
        print("local delete is not supported in HA mode; target a controller API with --server")
        return 2
    existing = store.get_registered_entry(ref.name)
    try:
        if existing is not None:
            store.delete_registered_app(
                ref.name,
                expected_resource_version=existing.resource_version,
            )
    except RegistryConflictError as exc:
        print(f"delete conflict: {exc}")
        return 1
    removed = runtime.remove_app(ref.name)
    ingress: IngressService | None = reconciler._ingress_service  # type: ignore[attr-defined]
    if ingress is not None:
        try:
            ingress.remove(ref.name)
            ingress.reload()
        except Exception:
            pass
    store.delete_app_state(ref.name, purge_history=bool(getattr(ns, "purge", False)))
    print(f"deleted {_display_app_name(ref.name)}: removed={removed} containers")
    return 0


def handle_scale_k1s(
    ns: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler
) -> int:
    ref = parse_ref(ns.ref, ("app",))
    revs = store.list_revisions(ref.name, limit=1)
    if not revs:
        print(
            f"No revisions recorded for {_display_app_name(ref.name)}. Try 'k1s apply -f <manifest>'."
        )
        return 1
    manifest = store.get_revision_manifest(ref.name, revs[0].revision)
    new_spec = manifest.spec.model_copy(update={"replicas": int(ns.replicas)})
    updated = manifest.model_copy(update={"spec": new_spec})
    try:
        existing = store.get_registered_entry(ref.name)
        src = existing.source if existing else "kctl"
        lbls = existing.labels if existing else getattr(updated.metadata, "labels", None)
        rv = store.register_app(
            updated,
            source=src,
            labels=lbls,
            expected_resource_version=(existing.resource_version if existing else None),
        )
        if _ha_mode_enabled():
            print(
                f"scaled desired state for {_display_app_name(ref.name)} to replicas={ns.replicas}: "
                f"resourceVersion={rv}"
            )
            return 0
    except RegistryConflictError as exc:
        print(f"scale conflict: {exc}")
        return 1
    except Exception:
        pass
    report = reconciler.reconcile(updated)
    print(
        f"scaled {_display_app_name(ref.name)} to replicas={ns.replicas}: rev={report.revision}({report.revision_status})"
    )
    return 0


def profile_lifecycle_manager_factory() -> LifecycleManager:
    """Return the concrete lifecycle manager behind the public CLI handler."""

    return LifecycleManager.from_environment()


def portable_profile_manager_factory() -> PortableProfileManager:
    """Return the installed portable manager only for an explicit lifecycle action."""

    return PortableProfileManager.system()


def profile_bootstrap_store_factory(profile: str) -> ProfileBootstrapStore:
    """Return the exact owner-private bootstrap store for a canonical role."""

    return ProfileBootstrapStore(ProfileRoots.from_environment(), profile)


def profile_engine_probe_factory() -> SelectedEngineProbe:
    """Return the bounded read-only probe only after ``doctor --engine``."""

    return SelectedEngineProbe.system()


def profile_doctor_probe_factory() -> ReadOnlyDoctorProbe:
    """Return the static installed-package probe used by ``profile doctor``."""

    return SystemDoctorProbe()


def profile_migration_manager_factory() -> ProfileMigrationManager:
    """Return the explicit migration manager without deriving HOME/XDG roots."""

    return ProfileMigrationManager()


def profile_host_transaction_manager_factory(
    request_fd: int, response_fd: int
) -> StrictCRIHostTransactionManager:
    """Bind only caller-inherited helper pipes; never discover or launch a helper."""

    return StrictCRIHostTransactionManager(
        PipeHostHelperClient(request_fd=request_fd, response_fd=response_fd)
    )


def _strict_cri_node_inputs(ns: argparse.Namespace) -> StrictCRINodeInputs | None:
    names = (
        "host_plan",
        "expect_host_plan_sha256",
        "expect_host_plan_digest",
        "host_receipt",
        "expect_host_receipt_sha256",
    )
    values = [getattr(ns, name, None) for name in names]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ProfileStateError("strict-CRI node authority is incomplete")
    return StrictCRINodeInputs(
        plan_path=Path(str(values[0])),
        expected_plan_file_digest=str(values[1]),
        expected_plan_digest=str(values[2]),
        receipt_path=Path(str(values[3])),
        expected_receipt_digest=str(values[4]),
    )


@dataclass(slots=True)
class _SignalRequest:
    signal_number: int | None = None

    def record(self, signal_number: int, frame: object) -> None:
        del frame
        if self.signal_number is None:
            self.signal_number = signal_number


@contextmanager
def _foreground_signal_requests() -> Iterator[_SignalRequest]:
    """Install handlers that only record intent; cleanup remains normal control flow."""

    request = _SignalRequest()
    previous = {
        signal_number: signal.getsignal(signal_number)
        for signal_number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for signal_number in previous:
            signal.signal(signal_number, request.record)
        yield request
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)


def handle_profile(ns: argparse.Namespace) -> int:
    """Inspect profile contracts or execute one explicit legacy lifecycle action."""

    if ns.profile_action == "host":
        try:
            if ns.host_action == "plan":
                payload = plan_strict_cri_from_files(
                    intent_path=ns.intent,
                    expected_intent_digest=ns.expect_intent_sha256,
                    facts_path=ns.facts,
                    expected_facts_digest=ns.expect_facts_sha256,
                )
                if ns.output == "json":
                    print(render_strict_cri_host_plan_json(payload), end="")
                else:
                    print(render_strict_cri_host_plan_text(payload), end="")
                return 0
            host_manager = profile_host_transaction_manager_factory(
                ns.helper_request_fd, ns.helper_response_fd
            )
            if ns.host_action == "prepare":
                host_result = host_manager.prepare(
                    intent_path=ns.intent,
                    expected_intent_digest=ns.expect_intent_sha256,
                    facts_path=ns.facts,
                    expected_facts_digest=ns.expect_facts_sha256,
                    plan_path=ns.plan,
                    expected_plan_file_digest=ns.expect_plan_sha256,
                    expected_plan_digest=ns.expect_plan_digest,
                    transaction_root=ns.transaction_root,
                    transaction_id=ns.transaction_id,
                    confirmation=ns.confirm_prepare,
                    expected_helper_identity_digest=ns.expect_helper_identity_sha256,
                )
            else:
                host_result = host_manager.rollback(
                    transaction_root=ns.transaction_root,
                    transaction_id=ns.transaction_id,
                    expected_applied_receipt_digest=ns.expect_applied_receipt_sha256,
                    confirmation=ns.confirm_rollback,
                    expected_helper_identity_digest=ns.expect_helper_identity_sha256,
                )
            result_payload = host_result.public_payload()
            if ns.output == "json":
                print(json.dumps(result_payload, sort_keys=True))
            else:
                print(
                    f"action={host_result.action} status={host_result.status} "
                    f"target={host_result.target_id} "
                    f"transaction={host_result.transaction_id} "
                    f"planDigest={host_result.plan_digest} "
                    f"receiptDigest={host_result.receipt_digest}"
                )
            return 0
        except (ProfileHostPlanError, ProfileHostTransactionError):
            operation = getattr(ns, "host_action", "plan")
            print(f"strict CRI host {operation} failed closed", file=sys.stderr)
            return 1

    if ns.profile_action == "migrate":
        migration_manager = profile_migration_manager_factory()
        try:
            if ns.migration_action == "plan":
                payload = migration_manager.plan(
                    ns.legacy_profile,
                    legacy_root=ns.legacy_root,
                    destination_profile=ns.destination_profile,
                    destination_root=ns.destination_root,
                ).public_payload()
            elif ns.migration_action == "apply":
                payload = migration_manager.apply(
                    ns.legacy_profile,
                    legacy_root=ns.legacy_root,
                    destination_profile=ns.destination_profile,
                    destination_root=ns.destination_root,
                    expected_plan_digest=ns.expect_plan_digest,
                ).public_payload()
            else:
                payload = migration_manager.rollback(
                    ns.legacy_profile,
                    legacy_root=ns.legacy_root,
                    destination_profile=ns.destination_profile,
                    destination_root=ns.destination_root,
                    migration_id=ns.migration_id,
                    expected_receipt_digest=ns.expect_receipt_digest,
                    confirmation=ns.confirm_rollback,
                ).public_payload()
            if ns.output == "json":
                print(json.dumps(payload, sort_keys=True))
            else:
                print(render_migration_text(payload))
            return 0
        except (OSError, ProfilePathError, ProfileStateError):
            print("profile migration failed closed", file=sys.stderr)
            return 1

    if ns.profile_action == "list":
        payload = profile_catalog_payload()
        if ns.output == "json":
            print(json.dumps(payload, sort_keys=True))
        else:
            print(render_profile_text(payload))
        return 0

    if ns.profile_action == "plan":
        payload = profile_plan_payload(ns.profile)
        if ns.output == "json":
            print(json.dumps(payload, sort_keys=True))
        else:
            print(render_profile_text(payload))
        return 0

    if ns.profile_action == "doctor":
        payload, exit_code = profile_doctor_payload(
            ns.profile,
            profile_doctor_probe_factory(),
            engine=ns.engine,
            engine_probe=(profile_engine_probe_factory() if ns.engine is not None else None),
        )
        if ns.output == "json":
            print(json.dumps(payload, sort_keys=True))
        else:
            print(render_profile_text(payload))
        return exit_code

    if ns.profile_action == "bootstrap":
        try:
            store = profile_bootstrap_store_factory(ns.bootstrap_profile)
            if ns.bootstrap_profile == "edge-gateway":
                record = store.create_edge_gateway(
                    site_id=ns.site_id,
                    node_id=ns.node_id,
                    transport_url=ns.transport_url,
                    credential_file=ns.credential_file,
                )
            else:
                record = store.create_node(
                    node_id=ns.node_id,
                    controller_url=ns.controller_url,
                    runtime_backend=ns.runtime_backend,
                    listen_host=ns.listen_host,
                    listen_port=ns.listen_port,
                    advertise_endpoint=ns.advertise_endpoint,
                    credential_file=ns.credential_file,
                )
            payload = record.public_projection()
            if ns.output == "json":
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    f"profile={payload['profile']} bootstrap={payload['bootstrapId']} "
                    f"node={payload['nodeId']}"
                )
            return 0
        except (OSError, ProfilePathError, ProfileStateError):
            print("profile bootstrap failed closed", file=sys.stderr)
            return 1

    if ns.profile_action in {"run", "logs", "delete"}:
        if (
            ns.profile_action == "run"
            and ns.profile in {"edge-gateway", "node"}
            and ns.bootstrap_id is None
        ):
            print("profile command failed closed", file=sys.stderr)
            return 1
        manager = portable_profile_manager_factory()
        try:
            strict_cri_inputs = _strict_cri_node_inputs(ns)
            if ns.profile_action == "run":
                with _foreground_signal_requests() as request:
                    if strict_cri_inputs is None:
                        run_result = manager.run(
                            ns.profile,
                            backend=ns.backend,
                            bootstrap_id=ns.bootstrap_id,
                            stop_requested=lambda: request.signal_number,
                        )
                    else:
                        run_result = manager.run(
                            ns.profile,
                            backend=ns.backend,
                            bootstrap_id=ns.bootstrap_id,
                            strict_cri_inputs=strict_cri_inputs,
                            stop_requested=lambda: request.signal_number,
                        )
                payload = run_result.to_dict()
                if ns.output == "json":
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print(
                        f"profile={run_result.profile} status={run_result.status} "
                        f"backend={run_result.backend} run={run_result.run_id}"
                    )
                return run_result.exit_code
            if ns.profile_action == "logs":
                snapshot = manager.logs(
                    ns.profile,
                    component=ns.component,
                    run_id=ns.run_id,
                    tail=ns.tail,
                )
                if ns.output == "json":
                    print(json.dumps(snapshot.to_payload(), sort_keys=True))
                else:
                    for line in snapshot.lines:
                        print(line)
                return 0
            delete_result = manager.delete(
                ns.profile,
                run_id=ns.run_id,
                confirmation=ns.confirm_delete,
            )
            payload = delete_result.to_dict()
            if ns.output == "json":
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    f"profile={delete_result.profile} status={delete_result.status} "
                    f"run={delete_result.run_id}"
                )
            return delete_result.exit_code
        except ProfileStateError:
            print("profile command failed closed", file=sys.stderr)
            return 1

    legacy = bool(ns.legacy)
    backend = getattr(ns, "backend", None)
    bootstrap_id = getattr(ns, "bootstrap_id", None)
    try:
        strict_cri_inputs = _strict_cri_node_inputs(ns)
    except ProfileStateError:
        print("profile command failed closed", file=sys.stderr)
        return 1
    result: PortableLifecycleResult | LifecycleResult
    if ns.profile in PORTABLE_PROFILES and not legacy:
        if (
            ns.profile_action in {"start", "restart"}
            and ns.profile in {"edge-gateway", "node"}
            and bootstrap_id is None
        ):
            print("profile command failed closed", file=sys.stderr)
            return 1
        if strict_cri_inputs is None:
            result = portable_profile_manager_factory().perform(
                ns.profile_action,
                ns.profile,
                backend=backend,
                bootstrap_id=bootstrap_id,
            )
        else:
            result = portable_profile_manager_factory().perform(
                ns.profile_action,
                ns.profile,
                backend=backend,
                bootstrap_id=bootstrap_id,
                strict_cri_inputs=strict_cri_inputs,
            )
    else:
        if ns.profile in PORTABLE_PROFILES:
            raise ValueError(f"{ns.profile} has no repository-local legacy lifecycle runner")
        if backend is not None or bootstrap_id is not None or strict_cri_inputs is not None:
            raise ValueError("portable execution options are unavailable to legacy profiles")
        result = profile_lifecycle_manager_factory().perform(ns.profile_action, ns.profile)
    result_payload = result.to_dict()
    if ns.output == "json":
        print(json.dumps(result_payload, sort_keys=True))
    else:
        details = [f"profile={result.profile}", f"status={result.status}"]
        pid = result_payload.get("pid")
        command = result_payload.get("command")
        if pid is not None:
            details.append(f"pid={pid}")
        if isinstance(command, (list, tuple)) and all(isinstance(item, str) for item in command):
            details.append(f"command={' '.join(command)}")
        if result_payload.get("backend"):
            details.append(f"backend={result_payload['backend']}")
        if result_payload.get("run_id"):
            details.append(f"run={result_payload['run_id']}")
        print(" ".join(details))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exercised via tests
    parser = build_parser()
    ns = parser.parse_args(argv)

    # Logging
    if ns.verbose:
        configure_logging("DEBUG")
    elif ns.log_level:
        configure_logging(ns.log_level.upper())
    else:
        configure_logging(None)

    if ns.cmd == "profile":
        return handle_profile(ns)

    store, reconciler, runtime = _setup()

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "get": lambda a: handle_get(a, store),
        "describe": lambda a: handle_describe(a, store),
        "apply": lambda a: handle_apply(a, reconciler),
        "rollout": lambda a: handle_rollout(a, store, reconciler),
        "logs": lambda a: handle_logs_k1s(a, store, runtime),
        "events": lambda a: handle_events_k1s(a, store),
        "delete": lambda a: handle_delete_k1s(a, store, reconciler, runtime),
        "scale": lambda a: handle_scale_k1s(a, store, reconciler),
    }

    handler = handlers.get(ns.cmd)
    if handler is None:
        parser.error(f"Unhandled command: {ns.cmd}")
        return 2
    return handler(ns)


if __name__ == "__main__":
    raise SystemExit(main())
# ruff: noqa
# ruff: noqa: E501,I001,F401,UP006,UP007,UP017,UP035,S110,S112
