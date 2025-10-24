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
from ae.observability import MetricsService
from ae.observability.logging import configure_logging
from ae.runtime import DockerRuntime, RegistryAuthProvider, RuntimeAdapter, StubRuntime
from ae.secrets import SecretManager
from ae.config.manager import ConfigManager
from ae import __version__ as AE_VERSION
from ae import build_info as AE_BUILD_INFO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae", description="Minimal application engine CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument("--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)")

    apply_parser = subparsers.add_parser("apply", help="Apply a manifest")
    apply_parser.add_argument("-f", "--file", type=Path, required=True, help="Path to manifest")

    status_parser = subparsers.add_parser("status", help="Show application status")
    status_parser.add_argument("name", nargs="?", help="Application name (omit to list all)")
    status_parser.add_argument(
        "--history", type=int, default=0, help="Show the most recent N probe evaluations"
    )
    status_parser.add_argument(
        "--events", action="store_true", help="Show recent events alongside status"
    )
    status_parser.add_argument(
        "--wide", action="store_true", help="Show additional details like resources and volumes"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text"
    )

    logs_parser = subparsers.add_parser("logs", help="Tail application logs")
    logs_parser.add_argument("name", help="Application name")
    logs_parser.add_argument("--follow", action="store_true", help="Stream logs continuously")
    logs_parser.add_argument("--container", help="Replica selector: index (e.g. 0) or replica id", default=None)
    logs_parser.add_argument("--revision", type=int, default=None, help="Filter by revision number")
    logs_parser.add_argument("--tail", type=int, default=None, help="Number of lines from the end of the logs")
    logs_parser.add_argument(
        "--since",
        default=None,
        help="Only return logs newer than a relative duration like 5m, 1h (or seconds)",
    )
    logs_parser.add_argument(
        "--since-time",
        dest="since_time",
        default=None,
        help="Only return logs after an absolute timestamp (RFC3339, e.g., 2025-10-23T12:00:00Z)",
    )

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

    registry_parser = subparsers.add_parser("registry", help="Manage registry credentials")
    registry_parser.add_argument("action", choices=["list"], help="Action to perform")

    metrics_parser = subparsers.add_parser("metrics", help="Show aggregated metrics")
    metrics_parser.add_argument("--json", action="store_true", help="Emit JSON output")

    events_parser = subparsers.add_parser("events", help="Show recent events")
    events_parser.add_argument("name", help="Application name")
    events_parser.add_argument("--limit", type=int, default=20)

    # config validate
    cfg_parser = subparsers.add_parser("config", help="Manage config resources")
    cfg_sub = cfg_parser.add_subparsers(dest="config_cmd", required=True)
    cfg_val = cfg_sub.add_parser("validate", help="Validate and show keys from a config file")
    cfg_val.add_argument("--file", "-f", type=Path, required=True)
    cfg_val.add_argument("--json", action="store_true", help="Emit JSON with keys")

    # secret validate
    sec_parser = subparsers.add_parser("secret", help="Manage secret resources")
    sec_sub = sec_parser.add_subparsers(dest="secret_cmd", required=True)
    sec_val = sec_sub.add_parser("validate", help="Validate and show keys from a secret (SOPS) file")
    sec_val.add_argument("--file", "-f", type=Path, required=True)
    sec_val.add_argument("--json", action="store_true", help="Emit JSON with keys")
    sec_enc = sec_sub.add_parser("encrypt", help="Encrypt a JSON/YAML file with sops (wrapper)")
    sec_enc.add_argument("--input", "-i", type=Path, required=True)
    sec_enc.add_argument("--output", "-o", type=Path, required=True)
    sec_dec = sec_sub.add_parser("decrypt", help="Decrypt a sops file (wrapper)")
    sec_dec.add_argument("--input", "-i", type=Path, required=True)
    sec_dec.add_argument("--output", "-o", type=Path, required=True)

    # delete <name> [--purge]
    delete_parser = subparsers.add_parser("delete", help="Delete an application (containers + status)")
    delete_parser.add_argument("name", help="Application name")
    delete_parser.add_argument("--purge", action="store_true", help="Also purge events and revisions history")

    # scale <name> --replicas N
    scale_parser = subparsers.add_parser("scale", help="Scale an application by reconciling replicas")
    scale_parser.add_argument("name", help="Application name")
    scale_parser.add_argument("--replicas", type=int, required=True)

    # backup/restore
    backup_parser = subparsers.add_parser("backup", help="Backup and restore state/specs")
    backup_sub = backup_parser.add_subparsers(dest="backup_cmd", required=True)

    backup_create = backup_sub.add_parser("create", help="Create a backup tar.gz of DB and specs")
    backup_create.add_argument("--output", required=True, help="Output tar.gz path")
    backup_create.add_argument("--db", default=None, help="Path to state DB (defaults AE_STATE_DB)")
    backup_create.add_argument("--specs", default=None, help="Specs directory (defaults AE_SPECS_DIR or specs)")

    backup_restore = backup_sub.add_parser("restore", help="Restore a backup tar.gz into a directory")
    backup_restore.add_argument("--input", required=True, help="Input tar.gz path")
    backup_restore.add_argument("--into", required=True, help="Target directory to extract into")
    
    backup_list = backup_sub.add_parser("list", help="List archive contents")
    backup_list.add_argument("--input", required=True, help="Input tar.gz path")
    
    backup_verify = backup_sub.add_parser("verify", help="Verify archive health and contents")
    backup_verify.add_argument("--input", required=True, help="Input tar.gz path")

    # version
    subparsers.add_parser("version", help="Show version and build info")

    return parser


def state_store_from_env() -> SQLiteStateStore:
    db_path = Path(os.getenv("AE_STATE_DB", "state/controller.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteStateStore(db_path)


def runtime_factory(registry_auth: RegistryAuthProvider | None = None) -> RuntimeAdapter:
    backend = os.getenv("AE_RUNTIME_BACKEND", "docker").lower()
    if backend == "stub":
        return StubRuntime()
    return DockerRuntime(registry_auth=registry_auth)


def health_manager_factory() -> HealthManager:
    return HealthManager()


def ingress_service_factory() -> IngressService | None:
    root = os.getenv("AE_CADDY_SITES")
    if root is not None and root.strip() == "":
        return None
    config_root = Path(root) if root else Path("ops/dev/caddy/sites")
    config_root.mkdir(parents=True, exist_ok=True)

    binary = os.getenv("AE_CADDY_BIN", "caddy")
    config_file_env = os.getenv("AE_CADDY_FILE")
    config_file = Path(config_file_env) if config_file_env else None
    container = os.getenv("AE_CADDY_CONTAINER") or None

    # Optional reload timeout to avoid hangs if docker exec blocks
    timeout_env = os.getenv("AE_CADDY_RELOAD_TIMEOUT", "10")
    try:
        reload_timeout = float(timeout_env) if timeout_env else None
    except ValueError:
        reload_timeout = 10.0

    manager = CaddyIngressManager(
        config_root=config_root,
        caddy_binary=binary,
        config_file=config_file,
        container=container,
        reload_timeout=reload_timeout,
    )
    return IngressService(manager)


def secret_manager_factory() -> SecretManager:
    allow_plaintext = os.getenv("AE_ALLOW_PLAINTEXT_SECRETS") == "1"
    return SecretManager(allow_plaintext=allow_plaintext)


def config_manager_factory() -> ConfigManager:
    return ConfigManager()


def registry_auth_factory() -> RegistryAuthProvider:
    return RegistryAuthProvider()


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

    # Logging
    if args.verbose:
        configure_logging("DEBUG")
    elif args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)

    # Fast path for commands that don't need full wiring
    if args.command == "version":
        return handle_version()

    store = state_store_from_env()
    registry_auth = registry_auth_factory()
    runtime = runtime_factory(registry_auth=registry_auth)
    health_manager = health_manager_factory()
    ingress_service = ingress_service_factory()
    secret_manager = secret_manager_factory()
    config_manager = config_manager_factory()
    reconciler = Reconciler(
        runtime=runtime,
        state_store=store,
        health_manager=health_manager,
        ingress_service=ingress_service,
        secret_manager=secret_manager,
        config_manager=config_manager,
    )

    command_handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "apply": lambda ns: handle_apply(ns, reconciler),
        "status": lambda ns: handle_status(ns, store),
        "logs": lambda ns: handle_logs(ns, store, runtime),
        "rollback": lambda ns: handle_rollback(ns, store, reconciler),
        "revisions": lambda ns: handle_revisions(ns, store),
        "registry": lambda ns: handle_registry(ns, registry_auth),
        "metrics": lambda ns: handle_metrics(ns, store),
        "events": lambda ns: handle_events(ns, store),
        "delete": lambda ns: handle_delete(ns, store, runtime, ingress_service),
        "scale": lambda ns: handle_scale(ns, store, reconciler),
        "backup": lambda ns: handle_backup(ns),
        "version": lambda ns: handle_version(),
        "config": lambda ns: handle_config(ns),
        "secret": lambda ns: handle_secret(ns),
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


def handle_config(ns: argparse.Namespace) -> int:
    if ns.config_cmd == "validate":
        from ae.config.manager import ConfigManager
        mgr = ConfigManager()
        data = mgr._load(ns.file)  # internal, safe for CLI
        keys = sorted(list(data.keys()))
        if ns.json:
            import json as _json
            print(_json.dumps({"file": str(ns.file), "keys": keys}, indent=2))
        else:
            print(f"config keys in {ns.file}:")
            for k in keys:
                print(f"  - {k}")
        return 0
    print(f"Unsupported config command: {ns.config_cmd}")
    return 1


def handle_secret(ns: argparse.Namespace) -> int:
    if ns.secret_cmd == "validate":
        from ae.secrets.manager import SecretManager
        mgr = SecretManager()
        # Use SecretRef adapter to reuse decrypt
        from ae.controller.spec import SecretRef, SecretEnvMapping
        dummy = SecretRef(name="cli", path=str(ns.file), env=[SecretEnvMapping(name="_", key="_")])
        # Call decrypt privately to get mapping
        try:
            data = mgr._decrypt(ns.file)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            print(f"decrypt failed: {exc}")
            return 1
        keys = sorted(list(map(str, data.keys())))
        if ns.json:
            import json as _json
            print(_json.dumps({"file": str(ns.file), "keys": keys}, indent=2))
        else:
            print(f"secret keys in {ns.file}:")
            for k in keys:
                print(f"  - {k}")
        return 0
    if ns.secret_cmd == "encrypt":
        # pass-through to sops -e -o
        from shutil import which
        sops = which("sops")
        if not sops:
            print("sops binary not found; install sops to use this wrapper")
            return 1
        import subprocess as sp
        try:
            sp.run([sops, "-e", "-o", str(ns.output), str(ns.input)], check=True)
            print(f"encrypted → {ns.output}")
            return 0
        except sp.CalledProcessError as exc:
            print(f"sops encrypt failed: {exc}")
            return 1
    if ns.secret_cmd == "decrypt":
        from shutil import which
        sops = which("sops")
        if not sops:
            print("sops binary not found; install sops to use this wrapper")
            return 1
        import subprocess as sp
        try:
            sp.run([sops, "-d", "-o", str(ns.output), str(ns.input)], check=True)
            print(f"decrypted → {ns.output}")
            return 0
        except sp.CalledProcessError as exc:
            print(f"sops decrypt failed: {exc}")
            return 1
    print(f"Unsupported secret command: {ns.secret_cmd}")
    return 1


def handle_delete(
    args: argparse.Namespace,
    store: SQLiteStateStore,
    runtime: RuntimeAdapter,
    ingress_service: IngressService | None,
) -> int:
    name = args.name
    removed = runtime.remove_app(name)
    if ingress_service:
        try:
            ingress_service.remove(name)
            ingress_service.reload()
        except Exception:
            pass
    store.delete_app_state(name, purge_history=bool(args.purge))
    print(f"deleted {name}: removed={removed} containers{' (purged history)' if args.purge else ''}")
    return 0


def handle_scale(args: argparse.Namespace, store: SQLiteStateStore, reconciler: Reconciler) -> int:
    name = args.name
    latest = store.list_revisions(name, limit=1)
    if not latest:
        print(f"No revisions recorded for {name}. Try 'ae apply -f <manifest>'.")
        return 1
    manifest = store.get_revision_manifest(name, latest[0].revision)
    updated_spec = manifest.spec.model_copy(update={"replicas": int(args.replicas)})
    new_manifest = manifest.model_copy(update={"spec": updated_spec})
    report = reconciler.reconcile(new_manifest)
    print(
        f"scaled {name} to replicas={args.replicas}: rev={report.revision}({report.revision_status}) "
        f"ops=+{report.created}/~{report.updated}/-{report.removed} ready={report.ready_replicas}/{report.live_replicas}"
    )
    return 0


def handle_version() -> int:
    info = AE_BUILD_INFO()
    print(f"ae {AE_VERSION} ({info['sha']} {info['date']})")
    return 0


def handle_backup(args: argparse.Namespace) -> int:
    import os
    import tarfile
    from datetime import datetime

    def _resolve_db() -> str:
        if getattr(args, "db", None):
            return args.db
        return os.getenv("AE_STATE_DB", "state/controller.db")

    def _resolve_specs() -> str:
        val = getattr(args, "specs", None)
        if val:
            return val
        return os.getenv("AE_SPECS_DIR", "specs")

    if args.backup_cmd == "create":
        db_path = _resolve_db()
        specs_dir = _resolve_specs()
        out = args.output
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with tarfile.open(out, "w:gz") as tar:
            if os.path.exists(db_path):
                tar.add(db_path, arcname="state/controller.db")
            if os.path.isdir(specs_dir):
                tar.add(specs_dir, arcname="specs")
        print(f"backup written: {out}")
        return 0

    if args.backup_cmd == "restore":
        src = args.input
        target = args.into
        os.makedirs(target, exist_ok=True)
        with tarfile.open(src, "r:gz") as tar:
            # Prefer tarfile.data_filter when available to strip metadata
            data_filter = getattr(tarfile, "data_filter", None)
            for m in tar.getmembers():
                name = m.name
                # Skip absolute paths and path traversal
                from pathlib import Path as _P
                parts = _P(name).parts
                if name.startswith("/") or ".." in parts:
                    continue
                if data_filter is not None:
                    tar.extract(m, path=target, filter=data_filter)
                else:
                    tar.extract(m, path=target)
        print(f"backup restored into: {target}")
        return 0

    if args.backup_cmd == "list":
        src = args.input
        with tarfile.open(src, "r:gz") as tar:
            for m in tar.getmembers():
                print(m.name)
        return 0

    if args.backup_cmd == "verify":
        import io
        src = args.input
        with tarfile.open(src, "r:gz") as tar:
            names = set(m.name for m in tar.getmembers())
            ok = True
            # state db optional but recommended
            if "state/controller.db" not in names:
                print("warning: state/controller.db not found in archive")
            if not any(n.startswith("specs/") for n in names):
                print("error: specs/ directory missing in archive")
                ok = False
            # quick integrity read of small member
            for m in list(tar.getmembers())[:3]:
                _ = tar.extractfile(m)
        print("verify: ok" if ok else "verify: failed")
        return 0 if ok else 1

    print(f"Unsupported backup command: {args.backup_cmd}")
    return 1


def handle_status(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    if args.name:
        status = store.get_status(args.name)
        if status is None:
            print(f"No status recorded for {args.name}")
            return 1
        if args.json:
            print(_status_to_json(status, store, include_details=args.wide))
            return 0
        print(format_status(status))
        if args.wide:
            try:
                manifest = store.get_revision_manifest(args.name, status.revision)
                res = manifest.spec.resources
                vols = manifest.spec.volumes
                if res and res.limits:
                    cpu = res.limits.cpu if res.limits.cpu is not None else "-"
                    mem = res.limits.memory if res.limits.memory is not None else "-"
                    print(f"    resources: limits cpu={cpu}, memory={mem}")
                if vols:
                    print(f"    volumes: {len(vols)} mounts")
            except Exception:
                pass
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
        if args.events:
            events = store.list_events(args.name, limit=10)
            if not events:
                print("    no events recorded")
            else:
                for event in events:
                    timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"    event {timestamp} rev={event.revision} "
                        f"{event.event_type}: {event.message}"
                    )
        return 0

    statuses = store.list_status()
    if not statuses:
        print("No applications recorded.")
        return 0
    if args.json:
        import json

        def as_dict(s: AppStatus) -> dict:
            return {
                "app_name": s.app_name,
                "desired_replicas": s.desired_replicas,
                "ready_replicas": s.ready_replicas,
                "live_replicas": s.live_replicas,
                "revision": s.revision,
                "revision_status": s.revision_status,
                "image": s.image,
                "ingress_host": s.ingress_host,
                "ingress_path": s.ingress_path,
            }

        print(json.dumps([as_dict(s) for s in statuses], indent=2))
        return 0

    for status in statuses:
        print(format_status(status))
    return 0


def handle_logs(args: argparse.Namespace, store: SQLiteStateStore, runtime: RuntimeAdapter) -> int:
    status = store.get_status(args.name)
    if status is None:
        print(f"No status recorded for {args.name}")
        return 1
    replicas = store.list_replicas(args.name)
    if not replicas:
        print(f"No replicas available for {args.name}")
        return 1
    # optional revision filter
    if args.revision is not None:
        rev_tag = f"-rev{args.revision}-"
        replicas = [r for r in replicas if rev_tag in r.replica_id]
        if not replicas:
            print(f"No replicas for {args.name} at revision {args.revision}")
            return 1

    # select by container flag
    target = None
    if args.container is not None:
        sel = str(args.container)
        if sel.isdigit():
            # match by replica index suffix
            suffix = f"-{sel}"
            for r in replicas:
                if r.replica_id.endswith(suffix):
                    target = r
                    break
        else:
            # exact match or contains
            for r in replicas:
                if r.replica_id == sel or sel in r.replica_id:
                    target = r
                    break
        if target is None:
            print(f"No matching replica for --container={sel}")
            return 1
    else:
        # prefer a ready replica, otherwise first
        target = next((r for r in replicas if r.ready), replicas[0])

    since_seconds = _parse_since_secs(args.since) if args.since else None
    if since_seconds is None and args.since_time:
        since_seconds = _parse_rfc3339_to_epoch(args.since_time)

    for line in runtime.read_logs(
        target.replica_id,
        follow=args.follow,
        tail=args.tail,
        since=since_seconds,
    ):
        print(line)
    return 0


def _status_to_json(status: AppStatus, store: SQLiteStateStore, *, include_details: bool) -> str:
    import json

    data = {
        "app_name": status.app_name,
        "desired_replicas": status.desired_replicas,
        "ready_replicas": status.ready_replicas,
        "live_replicas": status.live_replicas,
        "revision": status.revision,
        "revision_status": status.revision_status,
        "image": status.image,
        "ingress_host": status.ingress_host,
        "ingress_path": status.ingress_path,
    }
    if include_details:
        try:
            manifest = store.get_revision_manifest(status.app_name, status.revision)
            res = manifest.spec.resources
            vols = manifest.spec.volumes
            if res and res.limits:
                data["resources"] = {
                    "limits": {
                        "cpu": res.limits.cpu,
                        "memory": res.limits.memory,
                    }
                }
            if vols:
                data["volumes"] = [
                    {
                        "hostPath": v.host_path,
                        "mountPath": v.mount_path,
                        "readOnly": v.read_only,
                    }
                    for v in vols
                ]
        except Exception:
            pass
    return json.dumps(data, indent=2)


def _parse_since_secs(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v.isdigit():
        return int(v)
    try:
        # simple units: s, m, h
        num = ""
        unit = "s"
        for ch in v:
            if ch.isdigit():
                num += ch
            else:
                unit = ch
        n = int(num) if num else 0
        if unit == "h":
            return n * 3600
        if unit == "m":
            return n * 60
        return n
    except Exception:
        return None


def _parse_rfc3339_to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        import datetime as _dt
        from datetime import timezone as _tz

        s = value.strip()
        # Support trailing Z or offset like +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return int(dt.timestamp())
    except Exception:
        return None


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


def handle_registry(args: argparse.Namespace, provider: RegistryAuthProvider) -> int:
    if args.action == "list":
        registries = provider.list_registries()
        if not registries:
            print("No registry credentials configured.")
            return 0
        for host, creds in registries.items():
            user = creds.get("username", "")
            print(f"{host}: username={user}")
        return 0
    print(f"Unsupported registry action: {args.action}")
    return 1


def handle_metrics(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    service = MetricsService(store)
    snapshot = service.snapshot()
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "total_apps": snapshot.total_apps,
                    "ready_apps": snapshot.ready_apps,
                    "progressing_apps": snapshot.progressing_apps,
                    "degraded_apps": snapshot.degraded_apps,
                    "total_replicas": snapshot.total_replicas,
                    "ready_replicas": snapshot.ready_replicas,
                    "live_replicas": snapshot.live_replicas,
                },
                indent=2,
            )
        )
        return 0

    print(
        "apps total={total} ready={ready} progressing={progressing} degraded={degraded}".format(
            total=snapshot.total_apps,
            ready=snapshot.ready_apps,
            progressing=snapshot.progressing_apps,
            degraded=snapshot.degraded_apps,
        )
    )
    print(
        "replicas total={total} ready={ready} live={live}".format(
            total=snapshot.total_replicas,
            ready=snapshot.ready_replicas,
            live=snapshot.live_replicas,
        )
    )
    return 0


def handle_events(args: argparse.Namespace, store: SQLiteStateStore) -> int:
    events = store.list_events(args.name, limit=args.limit)
    if not events:
        print(f"No events recorded for {args.name}.")
        return 0
    for event in events:
        timestamp = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{timestamp} rev={event.revision} {event.event_type}: {event.message}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
