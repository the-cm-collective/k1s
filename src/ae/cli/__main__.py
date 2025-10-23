"""Command-line interface for the ae orchestrator."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable

from ae.controller.reconciler import ReconcileReport, Reconciler
from ae.controller.state import SQLiteStateStore
from ae.runtime import DockerRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae", description="Minimal application engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply a manifest")
    apply_parser.add_argument("-f", "--file", type=Path, required=True, help="Path to manifest")

    status_parser = subparsers.add_parser("status", help="Show application status")
    status_parser.add_argument("name", nargs="?", help="Application name (omit to list all)")

    logs_parser = subparsers.add_parser("logs", help="Tail application logs (stub)")
    logs_parser.add_argument("name", help="Application name")
    logs_parser.add_argument("--follow", action="store_true", help="Stream logs continuously")

    return parser


def state_store_from_env() -> SQLiteStateStore:
    db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(db_path)


def runtime_factory() -> DockerRuntime:
    return DockerRuntime()


def format_report(report: ReconcileReport) -> str:
    return (
        f"Applied {report.app_name}: +{report.created}/~{report.updated}/-{report.removed}, "
        f"ready={report.ready_replicas}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    store = state_store_from_env()
    runtime = runtime_factory()
    reconciler = Reconciler(runtime=runtime, state_store=store)

    command_handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "apply": lambda ns: handle_apply(ns, reconciler),
        "status": lambda ns: handle_status(ns, store),
        "logs": lambda ns: handle_logs(ns),
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
        print(
            f"{status.app_name}: desired={status.desired_replicas}, "
            f"ready={status.ready_replicas}, image={status.image}"
        )
        return 0

    statuses = store.list_status()
    if not statuses:
        print("No applications recorded.")
        return 0

    for status in statuses:
        print(
            f"{status.app_name}: desired={status.desired_replicas}, "
            f"ready={status.ready_replicas}, image={status.image}"
        )
    return 0


def handle_logs(args: argparse.Namespace) -> int:
    parts = ["Logs for", args.name]
    if args.follow:
        parts.append("streaming")
    parts.append("are not implemented yet.")
    print(" ".join(parts))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
