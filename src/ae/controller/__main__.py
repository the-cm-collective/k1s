"""Controller daemon entry point.

Usage:

  python -m ae.controller --once --specs specs/
  python -m ae.controller --loop --interval 5 --specs specs/ --metrics-port 9108

Polls the specs directory for manifests and reconciles all apps either once or
on a fixed interval. Optionally serves a tiny Prometheus text endpoint.
"""

from __future__ import annotations

import argparse
import os
import time
import signal
from pathlib import Path
from typing import Iterable, List

from ae.controller.reconciler import Reconciler
from ae.controller.spec import AppManifest, ManifestError, load_manifest
from ae.observability.http_api import start_http_api, set_reconcile_metrics
from ae.observability.logging import configure_logging
from ae.cli.__main__ import (
    state_store_from_env,
    runtime_factory,
    health_manager_factory,
    ingress_service_factory,
    secret_manager_factory,
    config_manager_factory,
    registry_auth_factory,
    format_report,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ae.controller", description="k1s controller daemon")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="Reconcile once and exit")
    group.add_argument("--loop", action="store_true", help="Run continuously and reconcile on an interval")
    p.add_argument("--specs", default=os.getenv("AE_SPECS_DIR", "specs"), help="Specs directory")
    p.add_argument("--interval", type=int, default=5, help="Polling interval in seconds for --loop")
    p.add_argument("--metrics-port", type=int, default=0, help="If set, serve Prometheus metrics on this TCP port")
    p.add_argument("--watch", action="store_true", help="Watch specs directory for changes (uses watchdog if available)")
    p.add_argument("--debounce-ms", type=int, default=200, help="Debounce time for watch events")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    p.add_argument("--log-level", default=None, help="Override log level (DEBUG/INFO/WARNING/ERROR)")
    return p


def _find_manifests(specs_dir: Path) -> List[Path]:
    paths: List[Path] = []
    if not specs_dir.exists():
        return paths
    for p in specs_dir.rglob("*.y*"):
        if p.is_file():
            paths.append(p)
    return sorted(paths)


def _load_all(paths: Iterable[Path]) -> List[AppManifest]:
    manifests: List[AppManifest] = []
    for path in paths:
        try:
            m = load_manifest(path)
        except ManifestError:
            continue
        manifests.append(m)
    return manifests


def _make_reconciler() -> Reconciler:
    store = state_store_from_env()
    registry_auth = registry_auth_factory()
    runtime = runtime_factory(registry_auth=registry_auth)
    health = health_manager_factory()
    ingress = ingress_service_factory()
    secrets = secret_manager_factory()
    configs = config_manager_factory()
    return Reconciler(runtime, store, health_manager=health, ingress_service=ingress, secret_manager=secrets, config_manager=configs)


def _reconcile_all(reconciler: Reconciler, manifests: Iterable[AppManifest]) -> None:
    import time as _t
    from ae.observability.http_api import record_app_reconcile

    for m in manifests:
        t0 = _t.time()
        report = reconciler.reconcile(m)
        dt = _t.time() - t0
        record_app_reconcile(
            m.metadata.name,
            dt,
            created=report.created,
            updated=report.updated,
            removed=report.removed,
        )
        print(format_report(report))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover (covered via unit test paths)
    args = build_parser().parse_args(argv)
    specs_dir = Path(args.specs)
    # logging setup
    if args.verbose:
        configure_logging("DEBUG")
    elif args.log_level:
        configure_logging(args.log_level.upper())
    else:
        configure_logging(None)

    # Build reconciler (runtime, ingress, secrets, store)
    reconciler = _make_reconciler()

    # Initialize HTTP API server (metrics/status/events) and optional mutators if requested
    api_server = None
    if args.metrics_port and args.metrics_port > 0:
        store = state_store_from_env()

        # Optional mutators wired via closures and gated at handler level
        def _scale(app: str, replicas: int):  # noqa: ANN001
            revs = store.list_revisions(app, limit=1)
            if not revs:
                raise RuntimeError(f"no revisions recorded for {app}")
            manifest = store.get_revision_manifest(app, revs[0].revision)
            new_spec = manifest.spec.model_copy(update={"replicas": int(replicas)})
            updated = manifest.model_copy(update={"spec": new_spec})
            report = reconciler.reconcile(updated)
            return {
                "app": app,
                "replicas": int(replicas),
                "revision": report.revision,
                "status": report.revision_status,
                "created": report.created,
                "updated": report.updated,
                "removed": report.removed,
            }

        def _delete(app: str, purge: bool):  # noqa: ANN001
            # Remove runtime containers
            runtime = reconciler._runtime  # type: ignore[attr-defined]
            removed = runtime.remove_app(app)
            # Remove ingress if present
            ingress = reconciler._ingress_service  # type: ignore[attr-defined]
            if ingress is not None:
                try:
                    ingress.remove(app)
                    ingress.reload()
                except Exception:
                    pass
            store.delete_app_state(app, purge_history=bool(purge))
            return {"app": app, "removed": removed, "purged": bool(purge)}

        def _logs(app: str, container: str | None, tail: int | None, since: int | None, follow: bool):
            reps = store.list_replicas(app)
            target = None
            if container:
                sel = str(container)
                for r in reps:
                    if r.replica_id == sel or sel in r.replica_id:
                        target = r
                        break
            if not target and reps:
                target = next((r for r in reps if r.ready), reps[0])
            if not target:
                return []
            return reconciler._runtime.read_logs(target.replica_id, follow=follow, tail=tail, since=since)

        import logging, errno
        try:
            api_server, assigned, _ = start_http_api(
                args.metrics_port, store, scale_fn=_scale, delete_fn=_delete, logs_fn=_logs
            )
            logging.getLogger(__name__).info("http api listening on port %s", assigned)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EADDRINUSE:
                logging.getLogger(__name__).warning(
                    "http api port %s already in use; continuing without API server",
                    args.metrics_port,
                )
                api_server = None
            else:
                raise

    if args.once:
        manifests = _load_all(_find_manifests(specs_dir))
        _reconcile_all(reconciler, manifests)
        return 0

    # loop mode
    stop = False
    def _graceful(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    # optional filesystem watch
    changed = True  # force initial reconcile
    observer = None
    last_full = 0.0
    if args.watch:
        try:
            from watchdog.observers import Observer  # type: ignore
            from watchdog.events import FileSystemEventHandler  # type: ignore

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):  # noqa: D401 - event callback
                    nonlocal changed
                    # Only react to YAML-like files
                    import os
                    _, ext = os.path.splitext(getattr(event, "src_path", ""))
                    if ext.lower() in {".yml", ".yaml"}:
                        changed = True

            observer = Observer()
            handler = Handler()
            observer.schedule(handler, str(specs_dir), recursive=True)
            observer.start()
            import logging
            logging.getLogger(__name__).info(
                "watching %s for changes (debounce=%sms)", specs_dir, args.debounce_ms
            )
        except Exception:
            observer = None  # fallback to interval polling
            import logging
            logging.getLogger(__name__).info(
                "watchdog not available; falling back to interval polling"
            )
    else:
        import logging
        logging.getLogger(__name__).info("polling every %ss (no file watch)", args.interval)

    try:
        while not stop:
            now = time.time()
            do_full = changed or (now - last_full) >= max(1, int(args.interval))
            if do_full:
                t0 = time.time()
                manifests = _load_all(_find_manifests(specs_dir))
                _reconcile_all(reconciler, manifests)
                t1 = time.time()
                set_reconcile_metrics(ts_seconds=t1, duration_seconds=(t1 - t0))
                last_full = now
                # debounce
                changed = False
                time.sleep(max(0.001, args.debounce_ms / 1000.0))
            else:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if api_server is not None:
            api_server.shutdown()
            api_server.server_close()
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=1)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
