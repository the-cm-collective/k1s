# ruff: noqa: E501
from __future__ import annotations

import contextlib
import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DB_PATH_DEFAULT = Path(os.getenv("AE_APISHIM_DB", "state/apishim.db"))


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

    def __init__(self, db_path: Path = DB_PATH_DEFAULT) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._ensure_dir()
        self._conn = sqlite3.connect(self.db_path.as_posix(), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._watchers: dict[tuple[str, str, str, str], list[queue.Queue]] = {}

    def _ensure_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS objects (
                  grp TEXT NOT NULL,
                  ver TEXT NOT NULL,
                  res TEXT NOT NULL,
                  ns  TEXT NOT NULL,
                  name TEXT NOT NULL,
                  metadata TEXT NOT NULL,
                  spec TEXT NOT NULL,
                  status TEXT NOT NULL,
                  rv INTEGER NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY (grp, ver, res, ns, name)
                )
                """
            )

    def _now(self) -> float:
        return time.time()

    def _next_rv(self) -> int:
        # Use a simple increasing counter sourced from max(rv)+1 to avoid extra tables.
        row = self._conn.execute("SELECT COALESCE(MAX(rv), 0) as m FROM objects").fetchone()
        return int(row["m"]) + 1

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
    ) -> K8sObject:
        with self._lock, self._conn:
            existed = self.get(group, version, resource, namespace, name) is not None
            rv = self._next_rv()
            now = self._now()
            status = status or {}
            ns_val = namespace or ""
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
        obj = K8sObject(group, version, resource, namespace, name, metadata, spec, status, rv)
        self._publish(obj, "MODIFIED" if existed else "ADDED")
        return obj

    def get(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> K8sObject | None:
        ns_val = namespace or ""
        row = self._conn.execute(
            """
            SELECT * FROM objects WHERE grp=? AND ver=? AND res=? AND ns=? AND name=?
            """,
            (group, version, resource, ns_val, name),
        ).fetchone()
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

    def list(self, group: str, version: str, resource: str, namespace: str | None | None) -> list[K8sObject]:
        ns_val = namespace or ""
        cur = self._conn.execute(
            """
            SELECT * FROM objects WHERE grp=? AND ver=? AND res=? AND ns=?
            ORDER BY name
            """,
            (group, version, resource, ns_val),
        )
        out: list[K8sObject] = []
        for row in cur.fetchall():
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
        cur = self._conn.execute(
            """
            SELECT * FROM objects WHERE grp=? AND ver=? AND res=?
            ORDER BY ns, name
            """,
            (group, version, resource),
        )
        out: list[K8sObject] = []
        for row in cur.fetchall():
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

    def delete(
        self, group: str, version: str, resource: str, namespace: str | None, name: str
    ) -> bool:
        with self._lock, self._conn:
            prev = self.get(group, version, resource, namespace, name)
            ns_val = namespace or ""
            cur = self._conn.execute(
                "DELETE FROM objects WHERE grp=? AND ver=? AND res=? AND ns=? AND name=?",
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
        # None namespace means cluster-wide watch for namespaced resources
        ns_key = "*" if namespace is None else (namespace or "")
        key = (group, version, resource, ns_key)
        q: queue.Queue = queue.Queue(maxsize=1024)
        with self._lock:
            self._watchers.setdefault(key, []).append(q)

        last_rv = since_rv or 0
        try:
            # initial state as ADDED events
            items = self.list_all(group, version, resource) if namespace is None else self.list(group, version, resource, namespace)
            for obj in items:
                if obj.resource_version >= last_rv:
                    last_rv = max(last_rv, obj.resource_version)
                    yield ("ADDED", obj)
            while True:
                try:
                    ev = q.get(timeout=heartbeat_seconds or 10_000_000)  # very long if no heartbeat
                    et, o = ev
                    if o.resource_version >= last_rv:
                        last_rv = max(last_rv, o.resource_version)
                        yield ev
                except queue.Empty:
                    if allow_bookmarks and heartbeat_seconds:
                        # Emit a bookmark-like event
                        yield ("BOOKMARK", K8sObject(group, version, resource, namespace, "", {}, {}, {"resourceVersion": last_rv}, last_rv))
                    # else loop to wait again
        finally:
            with self._lock:
                lst = self._watchers.get(key, [])
                if q in lst:
                    lst.remove(q)
                if not lst and key in self._watchers:
                    del self._watchers[key]

    def _publish(self, obj: K8sObject, ev_type: str) -> None:
        specific = (obj.group, obj.version, obj.resource, (obj.namespace or ""))
        wildcard = (obj.group, obj.version, obj.resource, "*")
        with self._lock:
            for q in self._watchers.get(specific, []) + self._watchers.get(wildcard, []):
                with contextlib.suppress(queue.Full):
                    q.put_nowait((ev_type, obj))
