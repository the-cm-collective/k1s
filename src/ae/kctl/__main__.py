"""kubectl-like CLI for working with the ae/k1s engine.

This provides familiar commands (`get`, `describe`, `apply`, `rollout`, `logs`, `events`)
that map onto the existing `ae.cli` functionality.
"""

from __future__ import annotations

import argparse
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
    state_store_from_env,
)
from ae.observability.logging import configure_logging
from ae.controller.reconciler import Reconciler
from ae.controller.state import SQLiteStateStore
from ae.ingress.service import IngressService


# ----------------------------
# Parsing helpers
# ----------------------------


@dataclass(slots=True)
class ParsedRef:
    kind: str
    name: str


def parse_ref(arg: str, expected: tuple[str, ...]) -> ParsedRef:
    """Parse resource reference like "app/echo" or provide NAME + kind default.

    - Accepts forms:
      * "app/NAME" or "apps/NAME"
      * just NAME (defaults to the first `expected` kind)
    - Kinds are normalized to singular: "app".
    """

    if "/" in arg:
        raw_kind, name = arg.split("/", 1)
        kind = raw_kind.lower()
    else:
        kind = expected[0]
        name = arg

    kind_norm = "app" if kind in {"app", "apps"} else kind
    if kind_norm not in expected:
        raise argparse.ArgumentTypeError(
            f"Unsupported resource kind '{kind}'. Expected one of: {', '.join(expected)}"
        )
    return ParsedRef(kind=kind_norm, name=name)


# ----------------------------
# CLI construction
# ----------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="k1s", description="kubectl-like CLI for k1s")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    p.add_argument("--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # k1s get apps|app <name?>
    get_p = sub.add_parser("get", help="List resources or show a resource")
    get_p.add_argument("resource", choices=["apps", "app"], help="Resource type")
    get_p.add_argument("name", nargs="?", help="Optional resource name")
    get_p.add_argument("-o", "--output", choices=["wide", "json"], default="wide")

    # k1s describe app/<name>
    desc_p = sub.add_parser("describe", help="Describe a resource")
    desc_p.add_argument("ref", help="Resource ref: app/NAME or NAME")

    # k1s apply -f <file>
    apply_p = sub.add_parser("apply", help="Apply a manifest file")
    apply_p.add_argument("-f", "--file", type=Path, required=True)

    # k1s rollout history app/<name> | k1s rollout undo app/<name> [--to-revision N]
    roll_p = sub.add_parser("rollout", help="Manage rollouts")
    roll_sub = roll_p.add_subparsers(dest="roll_cmd", required=True)

    hist_p = roll_sub.add_parser("history", help="Show rollout history for a resource")
    hist_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    hist_p.add_argument("--limit", type=int, default=10)

    undo_p = roll_sub.add_parser("undo", help="Rollback/undo to a previous revision")
    undo_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    undo_p.add_argument("--to-revision", type=int, default=None)

    # k1s logs app/<name> [-f]
    logs_p = sub.add_parser("logs", help="Show application logs")
    logs_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    logs_p.add_argument("-f", "--follow", action="store_true")
    logs_p.add_argument("--container", default=None, help="Replica selector: index or id")
    logs_p.add_argument("--revision", type=int, default=None, help="Filter by revision")
    logs_p.add_argument("--tail", type=int, default=None, help="Limit output lines")
    logs_p.add_argument("--since", default=None, help="Relative time window: 5m, 1h")
    logs_p.add_argument("--since-time", dest="since_time", default=None, help="RFC3339 timestamp")

    # bonus passthroughs
    events_p = sub.add_parser("events", help="Show recent events for an app")
    events_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    events_p.add_argument("--limit", type=int, default=20)

    # k1s delete app/<name>
    del_p = sub.add_parser("delete", help="Delete an app (containers + status)")
    del_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    del_p.add_argument("--purge", action="store_true", help="Also purge events and revisions")

    # k1s scale app/<name> --replicas N
    sc_p = sub.add_parser("scale", help="Scale an app by reconciling replicas")
    sc_p.add_argument("ref", help="Resource ref: app/NAME or NAME")
    sc_p.add_argument("--replicas", type=int, required=True)

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
    reconciler = Reconciler(
        runtime, store, health_manager=health, ingress_service=ingress, secret_manager=secrets
    )
    return store, reconciler, runtime


def handle_get(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    if ns.name:
        ref = parse_ref(f"{ns.resource}/{ns.name}", ("app",))
        status = store.get_status(ref.name)
        if status is None:
            print(f"No status recorded for {ref.name}")
            return 1
        print(format_status(status))
        return 0

    # list
    statuses = store.list_status()
    if not statuses:
        print("No applications recorded.")
        return 0
    for s in statuses:
        print(format_status(s))
    return 0


def handle_describe(ns: argparse.Namespace, store: SQLiteStateStore) -> int:
    ref = parse_ref(ns.ref, ("app",))
    status = store.get_status(ref.name)
    if status is None:
        print(f"No status recorded for {ref.name}")
        return 1
    print(format_status(status))

    replicas = store.list_replicas(ref.name)
    for r in replicas:
        print(
            f"  - {r.replica_id}: ready={r.ready} live={r.live} status={r.status} | "
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
    report = reconciler.reconcile_manifest_path(ns.file)
    print(
        f"applied {report.app_name} rev={report.revision}({report.revision_status}) "
        f"ops=+{report.created}/~{report.updated}/-{report.removed} "
        f"ready={report.ready_replicas} live={report.live_replicas}"
    )
    return 0


def handle_rollout(ns: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler) -> int:
    if ns.roll_cmd == "history":
        ref = parse_ref(ns.ref, ("app",))
        revs = store.list_revisions(ref.name, limit=ns.limit)
        if not revs:
            print(f"No revisions recorded for {ref.name}.")
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
            f"rolled back {ref.name} to revision {report.revision} ({report.revision_status})"
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
        print(f"No events recorded for {ref.name}.")
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
    removed = runtime.remove_app(ref.name)
    ingress: IngressService | None = reconciler._ingress_service  # type: ignore[attr-defined]
    if ingress is not None:
        try:
            ingress.remove(ref.name)
            ingress.reload()
        except Exception:
            pass
    store.delete_app_state(ref.name, purge_history=bool(getattr(ns, "purge", False)))
    print(f"deleted {ref.name}: removed={removed} containers")
    return 0


def handle_scale_k1s(ns: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler) -> int:
    ref = parse_ref(ns.ref, ("app",))
    revs = store.list_revisions(ref.name, limit=1)
    if not revs:
        print(f"No revisions recorded for {ref.name}. Try 'k1s apply -f <manifest>'.")
        return 1
    manifest = store.get_revision_manifest(ref.name, revs[0].revision)
    new_spec = manifest.spec.model_copy(update={"replicas": int(ns.replicas)})
    updated = manifest.model_copy(update={"spec": new_spec})
    report = reconciler.reconcile(updated)
    print(
        f"scaled {ref.name} to replicas={ns.replicas}: rev={report.revision}({report.revision_status})"
    )
    return 0


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
