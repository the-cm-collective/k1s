# ruff: noqa: E501,UP006,UP007,UP017
"""State persistence helpers backed by SQLite (default) or Postgres (optional)."""

from __future__ import annotations

import json
import os
import sqlite3
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # Optional Postgres backend
    import psycopg
except Exception:  # pragma: no cover - optional dependency
    psycopg = None  # type: ignore

from ae.controller.health import HealthReport
from ae.controller.spec import AppManifest, app_key_for_manifest
from ae.runtime import RuntimeResult


@dataclass(slots=True)
class AppStatus:
    """Latest reconcile snapshot for an application."""

    app_name: str
    desired_replicas: int
    ready_replicas: int
    live_replicas: int
    revision: int
    revision_status: str
    image: str
    created: int
    updated: int
    removed: int
    ingress_host: str | None = None
    ingress_path: str | None = None


@dataclass(slots=True)
class RegistryEntry:
    """Registered desired-state manifest for reconciliation."""

    app_name: str
    manifest: AppManifest
    spec_hash: str
    source: str
    labels: dict
    updated_at: datetime


@dataclass(slots=True)
class ReplicaStatus:
    """Status for a single replica in the state store."""

    replica_id: str
    ready: bool
    live: bool
    status: str
    readiness_message: str
    liveness_message: str
    exit_code: int | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class ProbeHistoryEntry:
    """Recorded probe evaluation for audit/history purposes."""

    replica_id: str
    check_time: datetime
    ready: bool
    live: bool
    readiness_message: str
    liveness_message: str


@dataclass(slots=True)
class AppEvent:
    """Event emitted during reconciliation or runtime changes."""

    app_name: str
    revision: int
    event_type: str
    message: str
    created_at: datetime


@dataclass(slots=True)
class ServiceRecord:
    """Service-level metadata such as ClusterIP and exposed ports."""

    app_name: str
    cluster_ip: str
    ports: dict


@dataclass(slots=True)
class ServiceEndpoint:
    """Endpoint backing a Service port."""

    app_name: str
    port: int
    ip: str
    target_port: int
    ready: bool


@dataclass(slots=True)
class ServiceListItem:
    """Brief view used for IP allocation."""

    app_name: str
    cluster_ip: str


@dataclass(slots=True)
class StorageBinding:
    """Mapping of a persistent volume to the node that owns it."""

    app_name: str
    volume_name: str
    node_id: str
    retention: str | None
    created_at: datetime


@dataclass(slots=True)
class NodeRecord:
    """Registered node information."""

    node_id: str
    name: str | None
    labels: dict
    taints: list
    backend: str | None
    endpoint: str | None
    pod_cidr: str | None
    wg_pubkey: str | None
    cordoned: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class NodeStatus:
    """Latest heartbeat/status for a node."""

    node_id: str
    status: str
    seen_at: datetime


@dataclass(slots=True)
class RevisionInfo:
    """Information about a stored application revision."""

    revision: int
    spec_hash: str
    status: str
    image: str
    created_at: datetime | None = None


class SQLiteStateStore:
    """Minimal state store; sqlite by default, Postgres via AE_STATE_DSN or dsn=."""

    def __init__(self, db_path: Path | None = None, *, dsn: str | None = None) -> None:
        self._dsn = dsn or os.getenv("AE_STATE_DSN")
        self._db_path = db_path or Path("state/controller.db")
        self.backend = "sqlite"
        if self._dsn:
            if psycopg is None:
                raise RuntimeError(
                    "psycopg is required for Postgres state store (install psycopg[binary])"
                )
            self.backend = "postgres"
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as conn:
            needs_reset = not self._schema_matches(
                conn,
                "app_status",
                [
                    "app_name",
                    "desired_replicas",
                    "ready_replicas",
                    "live_replicas",
                    "revision",
                    "revision_status",
                    "image",
                    "created",
                    "updated",
                    "removed",
                    "ingress_host",
                    "ingress_path",
                ],
            ) or not self._schema_matches(
                conn,
                "replica_status",
                [
                    "app_name",
                    "replica_id",
                    "ready",
                    "live",
                    "status",
                    "readiness_message",
                    "liveness_message",
                    "exit_code",
                    "finished_at",
                ],
            )
            if needs_reset:
                conn.execute("DROP TABLE IF EXISTS probe_history")
                conn.execute("DROP TABLE IF EXISTS replica_status")
                conn.execute("DROP TABLE IF EXISTS app_status")

            # Service tables are additive; drop and recreate only if schema mismatches.
            if not self._schema_matches(
                conn,
                "services",
                [
                    "app_name",
                    "cluster_ip",
                    "ports",
                    "created_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS services")
            if not self._schema_matches(
                conn,
                "service_endpoints",
                [
                    "app_name",
                    "port",
                    "ip",
                    "target_port",
                    "ready",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS service_endpoints")
            if not self._schema_matches(
                conn,
                "nodes",
                [
                    "node_id",
                    "name",
                    "labels",
                    "taints",
                    "backend",
                    "endpoint",
                    "pod_cidr",
                    "wg_pubkey",
                    "cordoned",
                    "created_at",
                    "updated_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS nodes")
            if not self._schema_matches(
                conn,
                "node_heartbeats",
                [
                    "node_id",
                    "status",
                    "seen_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS node_heartbeats")
            if not self._schema_matches(
                conn,
                "storage_bindings",
                [
                    "app_name",
                    "volume_name",
                    "node_id",
                    "retention",
                    "created_at",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS storage_bindings")
            auto_inc = (
                "INTEGER PRIMARY KEY AUTOINCREMENT"
                if self.backend == "sqlite"
                else "SERIAL PRIMARY KEY"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_status (
                    app_name TEXT PRIMARY KEY,
                    desired_replicas INTEGER NOT NULL,
                    ready_replicas INTEGER NOT NULL,
                    live_replicas INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    revision_status TEXT NOT NULL,
                    image TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    removed INTEGER NOT NULL,
                    ingress_host TEXT,
                    ingress_path TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_registry (
                    app_name TEXT PRIMARY KEY,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS replica_status (
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    live INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    readiness_message TEXT NOT NULL,
                    liveness_message TEXT NOT NULL,
                    exit_code INTEGER,
                    finished_at TEXT,
                    PRIMARY KEY (app_name, replica_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_history (
                    id %s,
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    check_time TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    live INTEGER NOT NULL,
                    readiness_message TEXT NOT NULL,
                    liveness_message TEXT NOT NULL
                )
                """
                % auto_inc
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS replica_nodes (
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (app_name, replica_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_revisions (
                    app_name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    image TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (app_name, revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_events (
                    id %s,
                    app_name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
                % auto_inc
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rollout_canary (
                    app_name TEXT PRIMARY KEY,
                    weight REAL NOT NULL,
                    next_step_at TEXT NOT NULL,
                    step REAL NOT NULL,
                    max REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS services (
                    app_name TEXT PRIMARY KEY,
                    cluster_ip TEXT NOT NULL,
                    ports TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS service_endpoints (
                    app_name TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    ip TEXT NOT NULL,
                    target_port INTEGER NOT NULL,
                    ready INTEGER NOT NULL,
                    PRIMARY KEY (app_name, port, ip)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    name TEXT,
                    labels TEXT DEFAULT '{}',
                    taints TEXT DEFAULT '[]',
                    backend TEXT,
                    endpoint TEXT,
                    pod_cidr TEXT,
                    wg_pubkey TEXT,
                    cordoned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS node_heartbeats (
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (node_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS storage_bindings (
                    app_name TEXT NOT NULL,
                    volume_name TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    retention TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (app_name, volume_name)
                )
                """
            )
            conn.commit()

    def _connect(self):
        if self.backend == "sqlite":
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        raw = psycopg.connect(self._dsn)  # type: ignore[arg-type]
        return _PgCompatConnection(raw)

    def _schema_matches(self, conn, table: str, expected_columns: list[str]) -> bool:
        if self.backend == "sqlite":
            info = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if info is None:
                return False
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            return columns == expected_columns
        # For Postgres we skip strict schema match; rely on CREATE IF NOT EXISTS.
        return True

    def record_snapshot(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
        revision: int,
        revision_status: str,
    ) -> None:
        state_by_id = {state.replica_id: state for state in runtime_result.replica_states}
        app_name = app_key_for_manifest(manifest)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_status(app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, ingress_host, ingress_path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    desired_replicas=excluded.desired_replicas,
                    ready_replicas=excluded.ready_replicas,
                    live_replicas=excluded.live_replicas,
                    revision=excluded.revision,
                    revision_status=excluded.revision_status,
                    image=excluded.image,
                    created=excluded.created,
                    updated=excluded.updated,
                    removed=excluded.removed,
                    ingress_host=excluded.ingress_host,
                    ingress_path=excluded.ingress_path
                """,
                (
                    app_name,
                    manifest.spec.replicas,
                    health_report.ready_replicas,
                    health_report.live_replicas,
                    revision,
                    revision_status,
                    manifest.spec.image,
                    runtime_result.created,
                    runtime_result.updated,
                    runtime_result.removed,
                    manifest.spec.ingress.host if manifest.spec.ingress else None,
                    manifest.spec.ingress.path if manifest.spec.ingress else None,
                ),
            )

            conn.execute(
                "DELETE FROM replica_status WHERE app_name = ?",
                (app_name,),
            )

            # Clean existing placements for this app; will be repopulated below
            conn.execute("DELETE FROM replica_nodes WHERE app_name = ?", (app_name,))

            rows = []
            for replica in health_report.replicas:
                state = state_by_id.get(replica.replica_id)
                rows.append(
                    (
                        app_name,
                        replica.replica_id,
                        int(replica.ready),
                        int(replica.live),
                        state.status if state else "unknown",
                        replica.readiness_message,
                        replica.liveness_message,
                        state.exit_code if state else None,
                        state.finished_at.isoformat() if state and state.finished_at else None,
                    )
                )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO replica_status(app_name, replica_id, ready, live, status, readiness_message, liveness_message, exit_code, finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )

            timestamp = datetime.now(timezone.utc).isoformat()
            history_rows = [
                (
                    app_name,
                    replica.replica_id,
                    timestamp,
                    int(replica.ready),
                    int(replica.live),
                    replica.readiness_message,
                    replica.liveness_message,
                )
                for replica in health_report.replicas
            ]
            if history_rows:
                conn.executemany(
                    """
                    INSERT INTO probe_history(app_name, replica_id, check_time, ready, live, readiness_message, liveness_message)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    history_rows,
                )
                for replica in health_report.replicas:
                    conn.execute(
                        """
                        DELETE FROM probe_history
                        WHERE id IN (
                            SELECT id FROM probe_history
                            WHERE app_name = ? AND replica_id = ?
                            ORDER BY id DESC
                            LIMIT -1 OFFSET 50
                        )
                        """,
                        (app_name, replica.replica_id),
                    )
            # Persist placement mapping when runtime result contains node_id hints
            node_rows = []
            for rs in runtime_result.replica_states:
                node_id = getattr(rs, "node_id", None)
                if not node_id:
                    continue
                node_rows.append(
                    (
                        app_name,
                        rs.replica_id,
                        node_id,
                        timestamp,
                    )
                )
            if node_rows:
                conn.executemany(
                    """
                    INSERT INTO replica_nodes(app_name, replica_id, node_id, updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(app_name, replica_id) DO UPDATE SET
                        node_id=excluded.node_id,
                        updated_at=excluded.updated_at
                    """,
                    node_rows,
                )
            conn.execute(
                """
                UPDATE app_revisions
                SET status = ?, image = ?, spec_hash = spec_hash
                WHERE app_name = ? AND revision = ?
                """,
                (revision_status, manifest.spec.image, app_name, revision),
            )
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, ingress_host, ingress_path
                FROM app_status WHERE app_name = ?
                """,
                (app_name,),
            ).fetchone()
            if row is None:
                return None
            return AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                live_replicas=row[3],
                revision=row[4],
                revision_status=row[5],
                image=row[6],
                created=row[7],
                updated=row[8],
                removed=row[9],
                ingress_host=row[10],
                ingress_path=row[11],
            )

    def list_status(self) -> list[AppStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, live_replicas, revision, revision_status, image, created, updated, removed, ingress_host, ingress_path
                FROM app_status ORDER BY app_name
                """
            ).fetchall()
        return [
            AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                live_replicas=row[3],
                revision=row[4],
                revision_status=row[5],
                image=row[6],
                created=row[7],
                updated=row[8],
                removed=row[9],
                ingress_host=row[10],
                ingress_path=row[11],
            )
            for row in rows
        ]

    def list_replicas(self, app_name: str) -> list[ReplicaStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT replica_id, ready, live, status, readiness_message, liveness_message, exit_code, finished_at
                FROM replica_status
                WHERE app_name = ?
                ORDER BY replica_id
                """,
                (app_name,),
            ).fetchall()
        items: list[ReplicaStatus] = []
        for row in rows:
            finished_at = None
            if row[7]:
                try:
                    finished_at = datetime.fromisoformat(row[7])
                except Exception:
                    finished_at = None
            items.append(
                ReplicaStatus(
                    replica_id=row[0],
                    ready=bool(row[1]),
                    live=bool(row[2]),
                    status=row[3],
                    readiness_message=row[4],
                    liveness_message=row[5],
                    exit_code=row[6] if row[6] is not None else None,
                    finished_at=finished_at,
                )
            )
        return items

    def list_replica_nodes(self, app_name: str) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rs.replica_id, rn.node_id, rs.ready, rs.live, rs.status, rs.readiness_message, rs.liveness_message
                FROM replica_status rs
                LEFT JOIN replica_nodes rn ON rs.app_name = rn.app_name AND rs.replica_id = rn.replica_id
                WHERE rs.app_name = ?
                ORDER BY rs.replica_id
                """,
                (app_name,),
            ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4], row[5], row[6]) for row in rows]

    def set_replica_nodes(self, app_name: str, placements: list[tuple[str, str]]) -> None:
        """Replace placement mapping for an app."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM replica_nodes WHERE app_name = ?", (app_name,))
            if placements:
                conn.executemany(
                    """
                    INSERT INTO replica_nodes(app_name, replica_id, node_id, updated_at)
                    VALUES(?,?,?,?)
                    """,
                    [(app_name, rid, nid, ts) for rid, nid in placements],
                )
            conn.commit()

    def get_probe_history(self, app_name: str, limit: int) -> list[ProbeHistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT replica_id, check_time, ready, live, readiness_message, liveness_message
                FROM probe_history
                WHERE app_name = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (app_name, limit),
            ).fetchall()
        entries: list[ProbeHistoryEntry] = []
        for row in rows:
            try:
                check_time = datetime.fromisoformat(row[1])
            except ValueError:
                check_time = datetime.fromtimestamp(0, tz=timezone.utc)
            entries.append(
                ProbeHistoryEntry(
                    replica_id=row[0],
                    check_time=check_time,
                    ready=bool(row[2]),
                    live=bool(row[3]),
                    readiness_message=row[4],
                    liveness_message=row[5],
                )
            )
        return entries

    def prepare_revision(self, manifest: AppManifest, spec_hash: str) -> tuple[int, bool]:
        app_name = app_key_for_manifest(manifest)
        latest = self._get_latest_revision(app_name)
        if latest and latest.spec_hash == spec_hash:
            return latest.revision, False

        next_revision = (latest.revision if latest else 0) + 1 if latest else 1
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_revisions(app_name, revision, spec_hash, spec_json, image, created_at, status)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(app_name, revision) DO NOTHING
                """,
                (
                    app_name,
                    next_revision,
                    spec_hash,
                    spec_json,
                    manifest.spec.image,
                    created_at,
                    "pending",
                ),
            )
            conn.commit()
        return next_revision, True

    def _manifest_hash(self, manifest: AppManifest) -> str:
        payload = json.dumps(
            manifest.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def register_app(
        self,
        manifest: AppManifest,
        *,
        source: str | None = None,
        labels: dict | None = None,
    ) -> None:
        spec_json = json.dumps(manifest.model_dump(by_alias=True), sort_keys=True)
        spec_hash = self._manifest_hash(manifest)
        updated_at = datetime.now(timezone.utc).isoformat()
        app_name = app_key_for_manifest(manifest)
        existing_source = None
        existing_labels: dict | None = None
        if source is None or labels is None:
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT source, labels FROM app_registry WHERE app_name = ?",
                        (app_name,),
                    ).fetchone()
                if row is not None:
                    existing_source = row[0]
                    try:
                        existing_labels = json.loads(row[1] or "{}")
                        if not isinstance(existing_labels, dict):
                            existing_labels = {}
                    except Exception:
                        existing_labels = {}
            except Exception:
                existing_source = None
                existing_labels = None
        labels_json = json.dumps(
            labels if labels is not None else (existing_labels or {}),
            sort_keys=True,
        )
        source_val = str(source or existing_source or "unknown")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_registry(app_name, spec_hash, spec_json, source, labels, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    spec_hash=excluded.spec_hash,
                    spec_json=excluded.spec_json,
                    source=excluded.source,
                    labels=excluded.labels,
                    updated_at=excluded.updated_at
                """,
                (
                    app_name,
                    spec_hash,
                    spec_json,
                    source_val,
                    labels_json,
                    updated_at,
                ),
            )
            conn.commit()

    def list_registered_apps(self) -> list[RegistryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, spec_hash, spec_json, source, labels, updated_at
                FROM app_registry
                ORDER BY app_name
                """
            ).fetchall()
        entries: list[RegistryEntry] = []
        for row in rows:
            entry = self._registry_entry_from_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    def list_registered_app_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT app_name FROM app_registry ORDER BY app_name").fetchall()
        return [row[0] for row in rows]

    def get_registered_entry(self, app_name: str) -> RegistryEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT app_name, spec_hash, spec_json, source, labels, updated_at
                FROM app_registry
                WHERE app_name = ?
                """,
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        return self._registry_entry_from_row(row)

    def get_registered_manifest(self, app_name: str) -> AppManifest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT spec_json FROM app_registry WHERE app_name = ?",
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        try:
            return AppManifest.model_validate_json(row[0])
        except Exception:
            return None

    def delete_registered_app(self, app_name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_registry WHERE app_name = ?", (app_name,))
            conn.commit()

    def _registry_entry_from_row(self, row) -> RegistryEntry | None:
        try:
            manifest = AppManifest.model_validate_json(row[2])
        except Exception:
            return None
        try:
            labels = json.loads(row[4] or "{}")
            if not isinstance(labels, dict):
                labels = {}
        except Exception:
            labels = {}
        try:
            updated = datetime.fromisoformat(row[5]) if row[5] else datetime.now(timezone.utc)
        except Exception:
            updated = datetime.now(timezone.utc)
        return RegistryEntry(
            app_name=row[0],
            manifest=manifest,
            spec_hash=row[1],
            source=row[3],
            labels=labels,
            updated_at=updated,
        )

    def _get_latest_revision(self, app_name: str) -> Optional[RevisionInfo]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT revision, spec_hash, status, image, created_at
                FROM app_revisions
                WHERE app_name = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        try:
            created = datetime.fromisoformat(row[4]) if row[4] else None
        except Exception:
            created = None
        return RevisionInfo(
            revision=row[0],
            spec_hash=row[1],
            status=row[2],
            image=row[3],
            created_at=created,
        )

    def get_revision_manifest(self, app_name: str, revision: int) -> AppManifest:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT spec_json
                FROM app_revisions
                WHERE app_name = ? AND revision = ?
                """,
                (app_name, revision),
            ).fetchone()
        if row is None:
            raise ValueError(f"No revision {revision} recorded for {app_name}")
        return AppManifest.model_validate_json(row[0])

    def list_revisions(self, app_name: str, limit: int = 10) -> list[RevisionInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT revision, spec_hash, status, image, created_at
                FROM app_revisions
                WHERE app_name = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (app_name, limit),
            ).fetchall()
        result: list[RevisionInfo] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row[4]) if row[4] else None
            except Exception:
                created = None
            result.append(
                RevisionInfo(
                    revision=row[0],
                    spec_hash=row[1],
                    status=row[2],
                    image=row[3],
                    created_at=created,
                )
            )
        return result

    def record_event(self, app_name: str, revision: int, event_type: str, message: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_events(app_name, revision, event_type, message, created_at)
                VALUES(?,?,?,?,?)
                """,
                (app_name, revision, event_type, message, created_at),
            )
            conn.commit()

    def list_events(self, app_name: str, limit: int = 20) -> list[AppEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT revision, event_type, message, created_at
                FROM app_events
                WHERE app_name = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (app_name, limit),
            ).fetchall()
        events: list[AppEvent] = []
        for row in rows:
            created = datetime.fromisoformat(row[3])
            events.append(
                AppEvent(
                    app_name=app_name,
                    revision=row[0],
                    event_type=row[1],
                    message=row[2],
                    created_at=created,
                )
            )
        return events

    def list_events_paginated(
        self, app_name: str, limit: int, offset: int
    ) -> tuple[list[AppEvent], int]:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM app_events WHERE app_name = ?",
                (app_name,),
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT revision, event_type, message, created_at
                FROM app_events
                WHERE app_name = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (app_name, limit, offset),
            ).fetchall()
        events: list[AppEvent] = []
        for row in rows:
            created = datetime.fromisoformat(row[3])
            events.append(
                AppEvent(
                    app_name=app_name,
                    revision=row[0],
                    event_type=row[1],
                    message=row[2],
                    created_at=created,
                )
            )
        return events, total

    # --- Canary rollout state ----------------------------------------------

    def get_canary_state(self, app_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT weight, next_step_at, step, max, updated_at
                FROM rollout_canary WHERE app_name = ?
                """,
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "weight": float(row[0]),
            "next_step_at": str(row[1]),
            "step": float(row[2]),
            "max": float(row[3]),
            "updated_at": str(row[4]),
        }

    def upsert_canary_state(
        self, app_name: str, *, weight: float, next_step_at: str, step: float, max_weight: float
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rollout_canary(app_name, weight, next_step_at, step, max, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET weight=excluded.weight, next_step_at=excluded.next_step_at, step=excluded.step, max=excluded.max, updated_at=excluded.updated_at
                """,
                (app_name, float(weight), next_step_at, float(step), float(max_weight), updated_at),
            )
            conn.commit()

    # --- Services / Service IPAM -------------------------------------------

    def upsert_service(self, app_name: str, cluster_ip: str, ports: dict) -> None:
        """Persist or update service metadata (ClusterIP + ports)."""
        created_at = datetime.now(timezone.utc).isoformat()
        ports_json = json.dumps(ports, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO services(app_name, cluster_ip, ports, created_at)
                VALUES(?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET cluster_ip=excluded.cluster_ip, ports=excluded.ports
                """,
                (app_name, cluster_ip, ports_json, created_at),
            )
            conn.commit()

    def delete_service(self, app_name: str) -> None:
        """Remove service metadata and endpoints for an app."""
        with self._connect() as conn:
            conn.execute("DELETE FROM service_endpoints WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM services WHERE app_name = ?", (app_name,))
            conn.commit()

    def get_service(self, app_name: str) -> ServiceRecord | None:
        """Return service metadata if present."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cluster_ip, ports FROM services WHERE app_name = ?", (app_name,)
            ).fetchone()
        if row is None:
            return None
        try:
            ports = json.loads(row[1]) if row[1] else {}
        except json.JSONDecodeError:
            ports = {}
        return ServiceRecord(app_name=app_name, cluster_ip=row[0], ports=ports)

    def upsert_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        """Replace endpoints for an app."""
        with self._connect() as conn:
            conn.execute("DELETE FROM service_endpoints WHERE app_name = ?", (app_name,))
            rows = [
                (ep.app_name, ep.port, ep.ip, ep.target_port, int(ep.ready)) for ep in endpoints
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO service_endpoints(app_name, port, ip, target_port, ready)
                    VALUES(?,?,?,?,?)
                    """,
                    rows,
                )
            conn.commit()

    def list_service_endpoints(self, app_name: str) -> list[ServiceEndpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, port, ip, target_port, ready
                FROM service_endpoints
                WHERE app_name = ?
                ORDER BY port, ip
                """,
                (app_name,),
            ).fetchall()
        return [
            ServiceEndpoint(
                app_name=row[0],
                port=int(row[1]),
                ip=row[2],
                target_port=int(row[3]),
                ready=bool(row[4]),
            )
            for row in rows
        ]

    # Compatibility helper for older callers/tests
    def record_service_endpoints(self, app_name: str, endpoints: list[ServiceEndpoint]) -> None:
        self.upsert_service_endpoints(app_name, endpoints)

    def list_services(self) -> list[ServiceListItem]:
        """List all services stored (cluster IP allocation helper)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT app_name, cluster_ip FROM services ORDER BY app_name"
            ).fetchall()
        return [ServiceListItem(app_name=row[0], cluster_ip=row[1]) for row in rows]

    # --- Nodes / heartbeats ---------------------------------------------

    def upsert_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        labels: dict | None = None,
        taints: list | None = None,
        backend: str | None = None,
        endpoint: str | None = None,
        pod_cidr: str | None = None,
        wg_pubkey: str | None = None,
        cordoned: bool | None = None,
    ) -> None:
        if cordoned is None:
            cordoned = self._get_node_cordoned(node_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO nodes(node_id, name, labels, taints, backend, endpoint, pod_cidr, wg_pubkey, cordoned, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET
                    name=excluded.name,
                    labels=excluded.labels,
                    taints=excluded.taints,
                    backend=excluded.backend,
                    endpoint=excluded.endpoint,
                    pod_cidr=excluded.pod_cidr,
                    wg_pubkey=excluded.wg_pubkey,
                    cordoned=excluded.cordoned,
                    updated_at=excluded.updated_at
                """,
                (
                    node_id,
                    name,
                    json.dumps(labels or {}, sort_keys=True),
                    json.dumps(taints or [], sort_keys=True),
                    backend,
                    endpoint,
                    pod_cidr,
                    wg_pubkey,
                    int(bool(cordoned)),
                    now,
                    now,
                ),
            )
            conn.commit()

    def record_heartbeat(self, node_id: str, status: str) -> None:
        seen = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO node_heartbeats(node_id, status, seen_at)
                VALUES(?,?,?)
                ON CONFLICT(node_id) DO UPDATE SET status=excluded.status, seen_at=excluded.seen_at
                """,
                (node_id, status, seen),
            )
            conn.commit()

    def _get_node_cordoned(self, node_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cordoned FROM nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return bool(row[0]) if row is not None else False

    def cordon_node(self, node_id: str, cordoned: bool = True) -> bool:
        """Mark a node as (un)cordoned for scheduling purposes."""
        with self._connect() as conn:
            res = conn.execute(
                "UPDATE nodes SET cordoned = ? WHERE node_id = ?",
                (int(bool(cordoned)), node_id),
            )
            conn.commit()
            return res.rowcount > 0

    def list_nodes(self) -> list[tuple[NodeRecord, NodeStatus | None]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT n.node_id, n.name, n.labels, n.taints, n.backend, n.endpoint, n.pod_cidr, n.wg_pubkey, n.created_at, n.updated_at,
                       n.cordoned, hb.status, hb.seen_at
                FROM nodes n
                LEFT JOIN node_heartbeats hb ON hb.node_id = n.node_id
                ORDER BY n.node_id
                """
            ).fetchall()
        result: list[tuple[NodeRecord, NodeStatus | None]] = []
        for row in rows:
            labels = {}
            taints = []
            try:
                labels = json.loads(row[2] or "{}")
            except Exception:
                labels = {}
            try:
                taints = json.loads(row[3] or "[]")
            except Exception:
                taints = []
            try:
                created = datetime.fromisoformat(row[8])
            except Exception:
                created = datetime.fromtimestamp(0, tz=timezone.utc)
            try:
                updated = datetime.fromisoformat(row[9])
            except Exception:
                updated = datetime.fromtimestamp(0, tz=timezone.utc)
            node = NodeRecord(
                node_id=row[0],
                name=row[1],
                labels=labels,
                taints=taints,
                backend=row[4],
                endpoint=row[5],
                pod_cidr=row[6],
                wg_pubkey=row[7],
                cordoned=bool(row[10]),
                created_at=created,
                updated_at=updated,
            )
            status = None
            if row[11] is not None:
                try:
                    seen_at = datetime.fromisoformat(row[12])
                except Exception:
                    seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
                status = NodeStatus(node_id=row[0], status=row[11], seen_at=seen_at)
            result.append((node, status))
        return result

    def get_node(self, node_id: str) -> tuple[NodeRecord, NodeStatus | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT n.node_id, n.name, n.labels, n.taints, n.backend, n.endpoint, n.pod_cidr, n.wg_pubkey, n.created_at, n.updated_at,
                       n.cordoned, hb.status, hb.seen_at
                FROM nodes n
                LEFT JOIN node_heartbeats hb ON hb.node_id = n.node_id
                WHERE n.node_id = ?
                """,
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        labels = {}
        taints = []
        try:
            labels = json.loads(row[2] or "{}")
        except Exception:
            labels = {}
        try:
            taints = json.loads(row[3] or "[]")
        except Exception:
            taints = []
        try:
            created = datetime.fromisoformat(row[8])
        except Exception:
            created = datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            updated = datetime.fromisoformat(row[9])
        except Exception:
            updated = datetime.fromtimestamp(0, tz=timezone.utc)
        node = NodeRecord(
            node_id=row[0],
            name=row[1],
            labels=labels,
            taints=taints,
            backend=row[4],
            endpoint=row[5],
            pod_cidr=row[6],
            wg_pubkey=row[7],
            cordoned=bool(row[10]),
            created_at=created,
            updated_at=updated,
        )
        status = None
        if row[11] is not None:
            try:
                seen_at = datetime.fromisoformat(row[12])
            except Exception:
                seen_at = datetime.fromtimestamp(0, tz=timezone.utc)
            status = NodeStatus(node_id=row[0], status=row[11], seen_at=seen_at)
        return node, status

    # --- Storage bindings ----------------------------------------------

    def upsert_storage_binding(
        self, app_name: str, volume_name: str, node_id: str, retention: str | None = None
    ) -> None:
        """Record that a persistent volume resides on a specific node."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO storage_bindings(app_name, volume_name, node_id, retention, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(app_name, volume_name) DO UPDATE SET
                    node_id=excluded.node_id,
                    retention=excluded.retention,
                    created_at=excluded.created_at
                """,
                (app_name, volume_name, node_id, retention, now),
            )
            conn.commit()

    def list_storage_bindings(self, app_name: str) -> list[StorageBinding]:
        """Return recorded bindings for an app's persistent volumes."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, volume_name, node_id, retention, created_at
                FROM storage_bindings
                WHERE app_name = ?
                ORDER BY volume_name
                """,
                (app_name,),
            ).fetchall()
        out: list[StorageBinding] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row[4])
            except Exception:
                created = datetime.fromtimestamp(0, tz=timezone.utc)
            out.append(
                StorageBinding(
                    app_name=row[0],
                    volume_name=row[1],
                    node_id=row[2],
                    retention=row[3],
                    created_at=created,
                )
            )
        return out

    def delete_storage_bindings(self, app_name: str) -> None:
        """Remove all bindings for an app (e.g., on delete)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM storage_bindings WHERE app_name = ?", (app_name,))
            conn.commit()

    # --- Admin / maintenance helpers ---
    def delete_app_state(self, app_name: str, *, purge_history: bool = False) -> None:
        """Remove status and replica rows for an app. Optionally purge events and revisions.

        Does not affect running containers; the runtime is responsible for removing them.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM replica_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM app_status WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM storage_bindings WHERE app_name = ?", (app_name,))
            if purge_history:
                conn.execute("DELETE FROM app_events WHERE app_name = ?", (app_name,))
                conn.execute("DELETE FROM app_revisions WHERE app_name = ?", (app_name,))
            conn.commit()


class _PgCompatConnection:
    """Light wrapper to allow sqlite-style '?' placeholders on psycopg connections."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq):
        sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.executemany(sql, seq)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


# ruff: noqa
