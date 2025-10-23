"""Command-line interface for the ae orchestrator."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable

from ae.controller.health import HealthManager
from ae.controller.reconciler import ReconcileReport, Reconciler
from ae.controller.state import AppStatus, SQLiteStateStore, RevisionInfo
from ae.ingress import CaddyIngressManager, IngressService
from ae.runtime import DockerRuntime, RuntimeAdapter, StubRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae", description="Minimal application engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply a manifest")
    apply_parser.add_argument("-f", "--file", type=Path, required=True, help="Path to manifest")

    status_parser = subparsers.add_parser("status", help="Show application status")
    status_parser.add_argument("name", nargs="?", help="Application name (omit to list all)")
    status_parser.add_argument(
        "--history", type=int, default=0, help="Show the most recent N probe evaluations"
    )

    logs_parser = subparsers.add_parser("logs", help="Tail application logs (stub)")
    logs_parser.add_argument("name", help="Application name")
    logs_parser.add_argument("--follow", action="store_true", help="Stream logs continuously")

    rollback_parser = subparsers.add_parser("rollback", help="Rollback an application revision")
    rollback_parser.add_argument("name", help="Application name")
    rollback_parser.add_argument(
        "--to",
        type=int,
        default=None,
        help="Target revision number (default: previous revision)",
    )

    revisions_parser = subparsers.add_parser("revisions", help="List stored revisions")
    revisions_parser.add_argument("name", help="Application name")
    revisions_parser.add_argument("--limit", type=int, default=10)

    return parser


def state_store_from_env() -> SQLiteStateStore:
    db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(db_path)


def runtime_factory() -> RuntimeAdapter:
    backend = os.getenv("AE_RUNTIME_BACKEND", "docker").lower()
    if backend == "stub":
        return StubRuntime()
    return DockerRuntime()


def health_manager_factory() -> HealthManager:
    return HealthManager()


def ingress_service_factory() -> IngressService | None:
    root = os.getenv("AE_CADDY_SITES")
    if root is not None and root.strip() == "":
        return None
    config_root = Path(root) if root else Path("ops/dev/caddy/sites")
    config_root.mkdir(parents=True, exist_ok=True)
    binary = os.getenv("AE_CADDY_BIN", "caddy")
    manager = CaddyIngressManager(config_root=config_root, caddy_binary=binary)
    return IngressService(manager)


def format_report(report: ReconcileReport) -> str:
    return (
        f"Applied {report.app_name}: +{report.created}/~{report.updated}/-{report.removed}, "
        f"ready={report.ready_replicas}, live={report.live_replicas}, "
        f"rev={report.revision}({report.revision_status})"
    )


def format_status(status: AppStatus) -> str:
    parts = [
        f"{status.app_name}: desired={status.desired_replicas}",
        f"ready={status.ready_replicas}",
        f"live={status.live_replicas}",
        f"rev={status.revision}({status.revision_status})",
        f"image={status.image}",
        f"ops=+{status.created}/~{status.updated}/-{status.removed}",
    ]
    if status.ingress_host:
        path = status.ingress_path or "/"
        parts.append(f"ingress={status.ingress_host}{path}")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    store = state_store_from_env()
    runtime = runtime_factory()
    health_manager = health_manager_factory()
    ingress_service = ingress_service_factory()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=store,
        health_manager=health_manager,
        ingress_service=ingress_service,
    )

    command_handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "apply": lambda ns: handle_apply(ns, reconciler),
        "status": lambda ns: handle_status(ns, store),
        "logs": lambda ns: handle_logs(ns),
        "rollback": lambda ns: handle_rollback(ns, store, reconciler),
        "revisions": lambda ns: handle_revisions(ns, store),
    }

    handler = command_handlers.get(args.command)
    if handler is None:
        parser.error(f"Unhandled command: {args.command}")
        return 2
    return handler(args)


def handle_apply(args: argparse.Namespace, reconciler: Reconciler) -> int:
    report = reconciler.reconcile_manifest_path(args.file)
    print(format_report(report))
    return 0


def handle_status(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    if args.name:
        status = store.get_status(args.name)
        if status is None:
            print(f"No status recorded for {args.name}")
            return 1
        print(format_status(status))
        replicas = store.list_replicas(args.name)
        for replica in replicas:
            print(
                f"  - {replica.replica_id}: ready={replica.ready} "
                f"live={replica.live} status={replica.status} | "
                f"readiness={replica.readiness_message}; "
                f"liveness={replica.liveness_message}"
            )
        if args.history and args.history > 0:
            history = store.get_probe_history(args.name, args.history)
            for entry in history:
                timestamp = entry.check_time.strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"    history {timestamp} {entry.replica_id}: ready={entry.ready} "
                    f"live={entry.live} | readiness={entry.readiness_message}; "
                    f"liveness={entry.liveness_message}"
                )
        return 0

    statuses = store.list_status()
    if not statuses:
        print("No applications recorded.")
        return 0

    for status in statuses:
        print(format_status(status))
    return 0


def handle_logs(args: argparse.Namespace) -> int:
    parts = ["Logs for", args.name]
    if args.follow:
        parts.append("streaming")
    parts.append("are not implemented yet.")
    print(" ".join(parts))
    return 0


def handle_rollback(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    reconciler: Reconciler,
) -> int:
    target_rev: int | None = args.to
    if target_rev is None:
        revisions = store.list_revisions(args.name, limit=2)
        if len(revisions) < 2:
            print("No previous revision to roll back to.")
            return 1
        target_rev = revisions[1].revision

    try:
        manifest = store.get_revision_manifest(args.name, target_rev)
    except ValueError as exc:
        print(str(exc))
        return 1

    report = reconciler.reconcile(manifest)
    print(
        f"Rolled back {args.name} to revision {report.revision} "
        f"({report.revision_status})"
    )
    return 0


def handle_revisions(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    revisions = store.list_revisions(args.name, limit=args.limit)
    if not revisions:
        print(f"No revisions recorded for {args.name}.")
        return 0
    for info in revisions:
        print(
            f"rev {info.revision}: status={info.status}, image={info.image}, "
            f"hash={info.spec_hash[:8]}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
