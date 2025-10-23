"""State persistence helpers backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ae.controller.health import HealthReport
from ae.controller.spec import AppManifest
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
class ReplicaStatus:
    """Status for a single replica in the state store."""

    replica_id: str
    ready: bool
    live: bool
    status: str
    readiness_message: str
    liveness_message: str


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
class RevisionInfo:
    """Information about a stored application revision."""

    revision: int
    spec_hash: str
    status: str
    image: str


class SQLiteStateStore:
    """Minimal SQLite-backed store for reconcile snapshots."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._initialize()

    def _initialize(self) -> None:
        with self._connect() as conn:
            if not self._schema_matches(
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
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS probe_history")
                conn.execute("DROP TABLE IF EXISTS replica_status")
                conn.execute("DROP TABLE IF EXISTS app_status")
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
                CREATE TABLE IF NOT EXISTS replica_status (
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    live INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    readiness_message TEXT NOT NULL,
                    liveness_message TEXT NOT NULL,
                    PRIMARY KEY (app_name, replica_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    check_time TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    live INTEGER NOT NULL,
                    readiness_message TEXT NOT NULL,
                    liveness_message TEXT NOT NULL
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _schema_matches(self, conn: sqlite3.Connection, table: str, expected_columns: list[str]) -> bool:
        info = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if info is None:
            return False
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return columns == expected_columns

    def record_snapshot(
        self,
        manifest: AppManifest,
        runtime_result: RuntimeResult,
        health_report: HealthReport,
        revision: int,
        revision_status: str,
    ) -> None:
        state_by_id = {state.replica_id: state for state in runtime_result.replica_states}

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
                    manifest.metadata.name,
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
                (manifest.metadata.name,),
            )

            rows = [
                (
                    manifest.metadata.name,
                    replica.replica_id,
                    int(replica.ready),
                    int(replica.live),
                    state_by_id.get(replica.replica_id, None).status
                    if state_by_id.get(replica.replica_id)
                    else "unknown",
                    replica.readiness_message,
                    replica.liveness_message,
                )
                for replica in health_report.replicas
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO replica_status(app_name, replica_id, ready, live, status, readiness_message, liveness_message)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    rows,
                )

            timestamp = datetime.now(timezone.utc).isoformat()
            history_rows = [
                (
                    manifest.metadata.name,
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
                        (manifest.metadata.name, replica.replica_id),
                    )
            conn.execute(
                """
                UPDATE app_revisions
                SET status = ?, image = ?, spec_hash = spec_hash
                WHERE app_name = ? AND revision = ?
                """,
                (revision_status, manifest.spec.image, manifest.metadata.name, revision),
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
                SELECT replica_id, ready, live, status, readiness_message, liveness_message
                FROM replica_status
                WHERE app_name = ?
                ORDER BY replica_id
                """,
                (app_name,),
            ).fetchall()
        return [
            ReplicaStatus(
                replica_id=row[0],
                ready=bool(row[1]),
                live=bool(row[2]),
                status=row[3],
                readiness_message=row[4],
                liveness_message=row[5],
            )
            for row in rows
        ]

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
        latest = self._get_latest_revision(manifest.metadata.name)
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
                    manifest.metadata.name,
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

    def _get_latest_revision(self, app_name: str) -> Optional[RevisionInfo]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT revision, spec_hash, status, image
                FROM app_revisions
                WHERE app_name = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (app_name,),
            ).fetchone()
        if row is None:
            return None
        return RevisionInfo(
            revision=row[0],
            spec_hash=row[1],
            status=row[2],
            image=row[3],
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
                SELECT revision, spec_hash, status, image
                FROM app_revisions
                WHERE app_name = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (app_name, limit),
            ).fetchall()
        return [
            RevisionInfo(revision=row[0], spec_hash=row[1], status=row[2], image=row[3])
            for row in rows
        ]

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
