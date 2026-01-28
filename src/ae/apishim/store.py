# ruff: noqa: E501
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Optional Postgres backend
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore

from ae.resources import loader as resource_loader

DB_PATH_DEFAULT = Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))
QUEUE_SIZE_DEFAULT = int(os.getenv("AE_APISHIM_WATCH_QUEUE_SIZE", "1024") or "1024")


@dataclass
class K8sObject:
    group: str
    version: str
    resource: str  # plural
    namespace: str | None
    name: str
    metadata: dict[str, Any]
    spec: dict[str, Any]
    status: dict[str, Any]
    resource_version: int


class ObjectStore:
    """SQLite-backed storage for k8s-like objects.

    Rows are keyed by (g,v,resource,namespace,name) with JSON columns for metadata/spec/status.
    A monotonically increasing resource_version enables basic change detection.
    """

    def __init__(
        self,
        db_path: Path = DB_PATH_DEFAULT,
        *,
        dsn: str | None = None,
        queue_size: int | None = None,
    ) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._outbox_enabled = False
        self._outbox_source_id = uuid.uuid4().hex
        self._outbox_last_id = 0
        self._outbox_poll_interval = float(
            os.getenv("AE_APISHIM_WATCH_OUTBOX_POLL", "0.5") or "0.5"
        )
        self._outbox_batch = int(os.getenv("AE_APISHIM_WATCH_OUTBOX_BATCH", "250") or "250")
        self._outbox_ttl = int(os.getenv("AE_APISHIM_WATCH_OUTBOX_TTL", "300") or "300")
        self._outbox_cleanup_interval = int(
            os.getenv("AE_APISHIM_WATCH_OUTBOX_CLEANUP", "30") or "30"
        )
        self._outbox_stop = threading.Event()
        self._outbox_thread: threading.Thread | None = None
        self._metrics_outbox_enqueued = 0
        self._metrics_outbox_consumed = 0
        self._metrics_outbox_skipped = 0
        self._metrics_outbox_errors = 0
        self._queue_size = queue_size or int(
            os.getenv("AE_APISHIM_WATCH_QUEUE_SIZE", str(QUEUE_SIZE_DEFAULT)) or "1024"
        )
        self._watchers: dict[tuple[str, str, str, str], list[queue.Queue]] = {}
        self._metrics_dropped: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._metrics_enqueued: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._metrics_watchers: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._metrics_queue_depth: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._metrics_streams_started: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._metrics_broadcasts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self.backend = "sqlite"
        self._tombstones: dict[tuple[str, str, str, str, str], float] = {}
        self._tombstone_ttl = int(os.getenv("AE_APISHIM_TOMBSTONE_TTL", "15") or "15")
        self._conn = None
        self._dsn = dsn or os.getenv("AE_APISHIM_DSN")
        if self._dsn:
            if self._dsn.startswith("postgres"):
                if psycopg is None:
                    raise RuntimeError(
                        "psycopg is required for Postgres backend (install psycopg[binary])"
                    )
                self.backend = "postgres"
                self._init_pg()
                self._start_outbox()
            else:
                # Treat non-postgres DSN as sqlite path for compatibility
                self.db_path = Path(self._dsn)
                self._init_sqlite()
        else:
            self._init_sqlite()

    def _ensure_dir(self) -> None:
        if self.backend == "sqlite":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _init_sqlite(self) -> None:
        self.backend = "sqlite"
        self._ensure_dir()
        self._conn = sqlite3.connect(self.db_path.as_posix(), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                resource_loader.load_text("sql", "apishim", "create_objects_sqlite.sql")
            )

    def _init_pg(self) -> None:
        assert psycopg is not None  # for type checkers
        self.backend = "postgres"
        self._conn = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)  # type: ignore[arg-type]
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(resource_loader.load_text("sql", "apishim", "create_objects_pg.sql"))
            cur.execute("CREATE SEQUENCE IF NOT EXISTS apishim_rv_seq START WITH 1 INCREMENT BY 1")
            cur.execute(resource_loader.load_text("sql", "apishim", "create_watch_events_pg.sql"))
            cur.execute(
                "CREATE INDEX IF NOT EXISTS watch_events_created_idx ON watch_events (created_at)"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS watch_events_rv_idx ON watch_events (rv)")
        self._outbox_enabled = True

    def _start_outbox(self) -> None:
        if not self._outbox_enabled:
            return
        self._outbox_last_id = self._fetch_outbox_max_id()
        self._outbox_thread = threading.Thread(
            target=self._poll_outbox, name="apishim-outbox", daemon=True
        )
        self._outbox_thread.start()

    def _fetch_outbox_max_id(self) -> int:
        if not self._outbox_enabled:
            return 0
        with self._lock, self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM watch_events")
            row = cur.fetchone()
        try:
            return int(row["m"]) if row else 0
        except Exception:
            return 0

    def _fetch_outbox_since(self, last_id: int) -> list[dict[str, Any]]:
        if not self._outbox_enabled:
            return []
        with self._lock, self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(
                """
                SELECT id, source, grp, ver, res, ns, name, ev_type, rv, payload
                FROM watch_events
                WHERE id > %s
                ORDER BY id
                LIMIT %s
                """,
                (last_id, self._outbox_batch),
            )
            rows = cur.fetchall()
        return list(rows or [])

    def _cleanup_outbox(self, now: float | None = None) -> None:
        if not self._outbox_enabled or self._outbox_ttl <= 0:
            return
        now = now or time.time()
        cutoff = now - self._outbox_ttl
        with self._lock, self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute("DELETE FROM watch_events WHERE created_at < %s", (cutoff,))

    def _poll_outbox(self) -> None:
        last_cleanup = time.time()
        while not self._outbox_stop.is_set():
            try:
                rows = self._fetch_outbox_since(self._outbox_last_id)
                if rows:
                    for row in rows:
                        row_id = None
                        try:
                            row_id = int(row["id"])
                        except Exception:
                            row_id = None
                        if row_id is None:
                            continue
                        self._outbox_last_id = max(self._outbox_last_id, row_id)
                        if row.get("source") == self._outbox_source_id:
                            self._metrics_outbox_skipped += 1
                            continue
                        payload = json.loads(row.get("payload") or "{}")
                        obj = K8sObject(
                            row.get("grp") or "",
                            row.get("ver") or "",
                            row.get("res") or "",
                            row.get("ns") or "",
                            row.get("name") or "",
                            payload.get("metadata") or {},
                            payload.get("spec") or {},
                            payload.get("status") or {},
                            int(row.get("rv") or 0),
                        )
                        self._publish_local(obj, row.get("ev_type") or "MODIFIED")
                        self._metrics_outbox_consumed += 1
                now = time.time()
                if (
                    self._outbox_ttl > 0
                    and self._outbox_cleanup_interval > 0
                    and (now - last_cleanup) >= self._outbox_cleanup_interval
                ):
                    self._cleanup_outbox(now=now)
                    last_cleanup = now
            except Exception:
                self._metrics_outbox_errors += 1
            time.sleep(self._outbox_poll_interval)

    def _now(self) -> float:
        return time.time()

    def _next_rv(self) -> int:
        if self.backend == "sqlite":
            row = self._conn.execute("SELECT COALESCE(MAX(rv), 0) as m FROM objects").fetchone()  # type: ignore[union-attr]
            return int(row["m"]) + 1
        # Postgres: rely on sequence for safe monotonic increment
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute("SELECT nextval('apishim_rv_seq') AS rv")
            row = cur.fetchone()
            return int(row["rv"])

    def _tombstone_key(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> tuple[str, str, str, str, str]:
        return (group, version, resource, namespace or "", name)

    def _tombstone_active(
        self, key: tuple[str, str, str, str, str], now: float | None = None
    ) -> bool:
        if not self._tombstones:
            return False
        now = now or time.time()
        exp = self._tombstones.get(key)
        if exp is None:
            return False
        if exp <= now:
            self._tombstones.pop(key, None)
            return False
        return True

    def upsert(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject:
        with self._lock:
            existed = self.get(group, version, resource, namespace, name) is not None
            rv = resource_version or self._next_rv()
            now = self._now()
            status = status or {}
            ns_val = namespace or ""
            if self.backend == "sqlite":
                with self._conn:  # type: ignore[union-attr]
                    self._conn.execute(
                        """
                        INSERT INTO objects (grp, ver, res, ns, name, metadata, spec, status, rv, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(grp, ver, res, ns, name)
                        DO UPDATE SET metadata=excluded.metadata, spec=excluded.spec, status=excluded.status,
                                      rv=excluded.rv, updated_at=excluded.updated_at
                        """,
                        (
                            group,
                            version,
                            resource,
                            ns_val,
                            name,
                            json.dumps(metadata, separators=(",", ":")),
                            json.dumps(spec, separators=(",", ":")),
                            json.dumps(status, separators=(",", ":")),
                            rv,
                            now,
                            now,
                        ),
                    )
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        """
                        INSERT INTO objects (grp, ver, res, ns, name, metadata, spec, status, rv, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(grp, ver, res, ns, name)
                        DO UPDATE SET metadata=excluded.metadata, spec=excluded.spec, status=excluded.status,
                                      rv=excluded.rv, updated_at=excluded.updated_at
                        """,
                        (
                            group,
                            version,
                            resource,
                            ns_val,
                            name,
                            json.dumps(metadata, separators=(",", ":")),
                            json.dumps(spec, separators=(",", ":")),
                            json.dumps(status, separators=(",", ":")),
                            rv,
                            now,
                            now,
                        ),
                    )
        obj = K8sObject(group, version, resource, namespace, name, metadata, spec, status, rv)
        self._publish(obj, "MODIFIED" if existed else "ADDED")
        return obj

    def upsert_if_not_deleted(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        name: str,
        metadata: dict[str, Any],
        spec: dict[str, Any],
        status: dict[str, Any] | None = None,
        resource_version: int | None = None,
    ) -> K8sObject | None:
        key = self._tombstone_key(group, version, resource, namespace, name)
        with self._lock:
            if self._tombstone_active(key):
                return None
            existed = self.get(group, version, resource, namespace, name) is not None
            rv = resource_version or self._next_rv()
            now = self._now()
            status = status or {}
            ns_val = namespace or ""
            if self.backend == "sqlite":
                with self._conn:  # type: ignore[union-attr]
                    self._conn.execute(
                        """
                        INSERT INTO objects (grp, ver, res, ns, name, metadata, spec, status, rv, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(grp, ver, res, ns, name)
                        DO UPDATE SET metadata=excluded.metadata, spec=excluded.spec, status=excluded.status,
                                      rv=excluded.rv, updated_at=excluded.updated_at
                        """,
                        (
                            group,
                            version,
                            resource,
                            ns_val,
                            name,
                            json.dumps(metadata, separators=(",", ":")),
                            json.dumps(spec, separators=(",", ":")),
                            json.dumps(status, separators=(",", ":")),
                            rv,
                            now,
                            now,
                        ),
                    )
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        """
                        INSERT INTO objects (grp, ver, res, ns, name, metadata, spec, status, rv, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(grp, ver, res, ns, name)
                        DO UPDATE SET metadata=excluded.metadata, spec=excluded.spec, status=excluded.status,
                                      rv=excluded.rv, updated_at=excluded.updated_at
                        """,
                        (
                            group,
                            version,
                            resource,
                            ns_val,
                            name,
                            json.dumps(metadata, separators=(",", ":")),
                            json.dumps(spec, separators=(",", ":")),
                            json.dumps(status, separators=(",", ":")),
                            rv,
                            now,
                            now,
                        ),
                    )
        obj = K8sObject(group, version, resource, namespace, name, metadata, spec, status, rv)
        self._publish(obj, "MODIFIED" if existed else "ADDED")
        return obj

    def get(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> K8sObject | None:
        ns_val = namespace or ""
        with self._lock:
            if self.backend == "sqlite":
                row = self._conn.execute(
                    """
                    SELECT * FROM objects WHERE grp=? AND ver=? AND res=? AND ns=? AND name=?
                    """,
                    (group, version, resource, ns_val, name),
                ).fetchone()  # type: ignore[union-attr]
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        """
                        SELECT * FROM objects WHERE grp=%s AND ver=%s AND res=%s AND ns=%s AND name=%s
                        """,
                        (group, version, resource, ns_val, name),
                    )
                    row = cur.fetchone()
        if not row:
            return None
        return K8sObject(
            row["grp"],
            row["ver"],
            row["res"],
            row["ns"],
            row["name"],
            json.loads(row["metadata"]),
            json.loads(row["spec"]),
            json.loads(row["status"]),
            int(row["rv"]),
        )

    def list(
        self, group: str, version: str, resource: str, namespace: str | None | None
    ) -> list[K8sObject]:
        ns_val = namespace or ""
        with self._lock:
            if self.backend == "sqlite":
                cur = self._conn.execute(
                    """
                    SELECT * FROM objects WHERE grp=? AND ver=? AND res=? AND ns=?
                    ORDER BY name
                    """,
                    (group, version, resource, ns_val),
                )  # type: ignore[union-attr]
                rows = cur.fetchall()
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        """
                        SELECT * FROM objects WHERE grp=%s AND ver=%s AND res=%s AND ns=%s
                        ORDER BY name
                        """,
                        (group, version, resource, ns_val),
                    )
                    rows = cur.fetchall()
        out: list[K8sObject] = []
        for row in rows:
            out.append(
                K8sObject(
                    row["grp"],
                    row["ver"],
                    row["res"],
                    row["ns"],
                    row["name"],
                    json.loads(row["metadata"]),
                    json.loads(row["spec"]),
                    json.loads(row["status"]),
                    int(row["rv"]),
                )
            )
        return out

    def list_all(self, group: str, version: str, resource: str) -> list[K8sObject]:
        try:
            with self._lock:
                if self.backend == "sqlite":
                    cur = self._conn.execute(
                        """
                        SELECT * FROM objects WHERE grp=? AND ver=? AND res=?
                        ORDER BY ns, name
                        """,
                        (group, version, resource),
                    )  # type: ignore[union-attr]
                    rows = cur.fetchall()
                else:
                    with self._conn.cursor() as cur:  # type: ignore[union-attr]
                        cur.execute(
                            """
                            SELECT * FROM objects WHERE grp=%s AND ver=%s AND res=%s
                            ORDER BY ns, name
                            """,
                            (group, version, resource),
                        )
                        rows = cur.fetchall()
        except Exception:
            rows = []
        out: list[K8sObject] = []
        for row in rows:
            row_data = dict(row)
            meta = row_data.get("metadata")
            spec = row_data.get("spec")
            status = row_data.get("status")
            if meta is None or spec is None or status is None:
                continue
            out.append(
                K8sObject(
                    row_data["grp"],
                    row_data["ver"],
                    row_data["res"],
                    row_data["ns"],
                    row_data["name"],
                    json.loads(meta),
                    json.loads(spec),
                    json.loads(status),
                    int(row_data["rv"]),
                )
            )
        return out

    def delete(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> bool:
        with self._lock:
            prev = self.get(group, version, resource, namespace, name)
            if self._tombstone_ttl > 0:
                self._tombstones[self._tombstone_key(group, version, resource, namespace, name)] = (
                    self._now() + self._tombstone_ttl
                )
            ns_val = namespace or ""
            if self.backend == "sqlite":
                with self._conn:  # type: ignore[union-attr]
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        "DELETE FROM objects WHERE grp=? AND ver=? AND res=? AND ns=? AND name=?",
                        (group, version, resource, ns_val, name),
                    )
                    ok = cur.rowcount > 0
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        "DELETE FROM objects WHERE grp=%s AND ver=%s AND res=%s AND ns=%s AND name=%s",
                        (group, version, resource, ns_val, name),
                    )
                    ok = cur.rowcount > 0
            if ok and prev is not None:
                self._publish(prev, "DELETED")
            return ok

    def watch(
        self,
        group: str,
        version: str,
        resource: str,
        namespace: str | None,
        heartbeat_seconds: int | None = None,
        allow_bookmarks: bool = False,
        since_rv: int | None = None,
    ):
        ns_key = "*" if namespace is None else (namespace or "")
        key = (group, version, resource, ns_key)
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._watchers.setdefault(key, []).append(q)
            self._metrics_watchers[key] += 1
            self._metrics_streams_started[key] += 1
            self._metrics_queue_depth[key] = max(self._metrics_queue_depth[key], q.qsize())

        last_rv = since_rv or 0

        def _iter():
            nonlocal last_rv
            try:
                items = (
                    self.list_all(group, version, resource)
                    if namespace is None
                    else self.list(group, version, resource, namespace)
                )
                for obj in items:
                    if obj.resource_version >= last_rv:
                        last_rv = max(last_rv, obj.resource_version)
                        yield ("ADDED", obj)
                while True:
                    try:
                        ev = q.get(timeout=heartbeat_seconds or 10_000_000)
                        et, o = ev
                        self._metrics_queue_depth[key] = q.qsize()
                        if o.resource_version >= last_rv:
                            last_rv = max(last_rv, o.resource_version)
                            yield ev
                    except queue.Empty:
                        if allow_bookmarks and heartbeat_seconds:
                            yield (
                                "BOOKMARK",
                                K8sObject(
                                    group,
                                    version,
                                    resource,
                                    namespace,
                                    "",
                                    {},
                                    {},
                                    {"resourceVersion": last_rv},
                                    last_rv,
                                ),
                            )
            finally:
                with self._lock:
                    lst = self._watchers.get(key, [])
                    if q in lst:
                        lst.remove(q)
                    if not lst and key in self._watchers:
                        del self._watchers[key]
                    self._metrics_watchers[key] = max(self._metrics_watchers[key] - 1, 0)
                    self._metrics_queue_depth[key] = q.qsize()

        return _iter()

    def _publish(self, obj: K8sObject, ev_type: str) -> None:
        self._publish_local(obj, ev_type)
        self._enqueue_outbox(obj, ev_type)

    def _publish_local(self, obj: K8sObject, ev_type: str) -> None:
        specific = (obj.group, obj.version, obj.resource, (obj.namespace or ""))
        wildcard = (obj.group, obj.version, obj.resource, "*")
        with self._lock:
            for key in (specific, wildcard):
                for q in self._watchers.get(key, []):
                    try:
                        q.put_nowait((ev_type, obj))
                        self._metrics_enqueued[key] += 1
                        self._metrics_queue_depth[key] = q.qsize()
                    except queue.Full:
                        self._metrics_dropped[key] += 1
                self._metrics_broadcasts[key] += 1

    def _enqueue_outbox(self, obj: K8sObject, ev_type: str) -> None:
        if not self._outbox_enabled:
            return
        payload = json.dumps(
            {"metadata": obj.metadata, "spec": obj.spec, "status": obj.status},
            separators=(",", ":"),
        )
        ns_val = obj.namespace or ""
        try:
            with self._lock, self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    """
                    INSERT INTO watch_events
                      (source, grp, ver, res, ns, name, ev_type, rv, payload, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self._outbox_source_id,
                        obj.group,
                        obj.version,
                        obj.resource,
                        ns_val,
                        obj.name,
                        ev_type,
                        int(obj.resource_version),
                        payload,
                        self._now(),
                    ),
                )
            self._metrics_outbox_enqueued += 1
        except Exception:
            self._metrics_outbox_errors += 1

    def export_all(self) -> Iterable[K8sObject]:
        """Export all stored objects (used for migrations)."""
        with self._lock:
            if self.backend == "sqlite":
                cur = self._conn.execute("SELECT * FROM objects")  # type: ignore[union-attr]
                rows = cur.fetchall()
            else:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute("SELECT * FROM objects")
                    rows = cur.fetchall()
        for row in rows:
            yield K8sObject(
                row["grp"],
                row["ver"],
                row["res"],
                row["ns"],
                row["name"],
                json.loads(row["metadata"]),
                json.loads(row["spec"]),
                json.loads(row["status"]),
                int(row["rv"]),
            )

    def render_metrics(self) -> str:
        """Render Prometheus text metrics for watch/backpressure state."""
        lines = []
        lines.append("# HELP apishim_store_backend_info Backend in use for shim object store")
        lines.append("# TYPE apishim_store_backend_info gauge")
        lines.append(f'apishim_store_backend_info{{backend="{self.backend}"}} 1')

        def _labels(key: tuple[str, str, str, str]) -> str:
            g, v, r, ns = key
            return f'group="{g}",version="{v}",resource="{r}",namespace="{ns}"'

        if self._metrics_watchers:
            lines.append("# HELP apishim_watchers Active watch streams per resource")
            lines.append("# TYPE apishim_watchers gauge")
            for k, v in self._metrics_watchers.items():
                lines.append(f"apishim_watchers{{{_labels(k)}}} {v}")
        if self._metrics_queue_depth:
            lines.append("# HELP apishim_watch_queue_depth Current queue depth per watch key")
            lines.append("# TYPE apishim_watch_queue_depth gauge")
            for k, v in self._metrics_queue_depth.items():
                lines.append(f"apishim_watch_queue_depth{{{_labels(k)}}} {v}")
        if self._metrics_enqueued:
            lines.append(
                "# HELP apishim_watch_events_enqueued_total Events enqueued to watch queues"
            )
            lines.append("# TYPE apishim_watch_events_enqueued_total counter")
            for k, v in self._metrics_enqueued.items():
                lines.append(f"apishim_watch_events_enqueued_total{{{_labels(k)}}} {v}")
        if self._metrics_dropped:
            lines.append(
                "# HELP apishim_watch_events_dropped_total Events dropped due to backpressure"
            )
            lines.append("# TYPE apishim_watch_events_dropped_total counter")
            for k, v in self._metrics_dropped.items():
                lines.append(f"apishim_watch_events_dropped_total{{{_labels(k)}}} {v}")
        if self._metrics_broadcasts:
            lines.append("# HELP apishim_watch_broadcasts_total Publish attempts per watch key")
            lines.append("# TYPE apishim_watch_broadcasts_total counter")
            for k, v in self._metrics_broadcasts.items():
                lines.append(f"apishim_watch_broadcasts_total{{{_labels(k)}}} {v}")
        if self._metrics_streams_started:
            lines.append("# HELP apishim_watch_streams_started_total Watch streams started")
            lines.append("# TYPE apishim_watch_streams_started_total counter")
            for k, v in self._metrics_streams_started.items():
                lines.append(f"apishim_watch_streams_started_total{{{_labels(k)}}} {v}")
        if self._outbox_enabled:
            lines.append(
                "# HELP apishim_watch_outbox_enqueued_total Events written to the Postgres watch outbox"
            )
            lines.append("# TYPE apishim_watch_outbox_enqueued_total counter")
            lines.append(f"apishim_watch_outbox_enqueued_total {self._metrics_outbox_enqueued}")
            lines.append(
                "# HELP apishim_watch_outbox_consumed_total Events consumed from the Postgres watch outbox"
            )
            lines.append("# TYPE apishim_watch_outbox_consumed_total counter")
            lines.append(f"apishim_watch_outbox_consumed_total {self._metrics_outbox_consumed}")
            lines.append(
                "# HELP apishim_watch_outbox_skipped_total Outbox events skipped (originated locally)"
            )
            lines.append("# TYPE apishim_watch_outbox_skipped_total counter")
            lines.append(f"apishim_watch_outbox_skipped_total {self._metrics_outbox_skipped}")
            lines.append("# HELP apishim_watch_outbox_errors_total Outbox poll/insert errors")
            lines.append("# TYPE apishim_watch_outbox_errors_total counter")
            lines.append(f"apishim_watch_outbox_errors_total {self._metrics_outbox_errors}")
            lines.append("# HELP apishim_watch_outbox_last_id Last processed outbox id")
            lines.append("# TYPE apishim_watch_outbox_last_id gauge")
            lines.append(f"apishim_watch_outbox_last_id {self._outbox_last_id}")
        return "\n".join(lines) + "\n"
