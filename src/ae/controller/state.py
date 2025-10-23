"""State persistence helpers backed by SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
    image: str
    created: int
    updated: int
    removed: int
    ingress_host: str | None = None
    ingress_path: str | None = None
    ingress_host: str | None
    ingress_path: str | None


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
    ) -> None:
        state_by_id = {state.replica_id: state for state in runtime_result.replica_states}

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_status(app_name, desired_replicas, ready_replicas, live_replicas, image, created, updated, removed, ingress_host, ingress_path)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    desired_replicas=excluded.desired_replicas,
                    ready_replicas=excluded.ready_replicas,
                    live_replicas=excluded.live_replicas,
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
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, live_replicas, image, created, updated, removed, ingress_host, ingress_path
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
                image=row[4],
                created=row[5],
                updated=row[6],
                removed=row[7],
                ingress_host=row[8],
                ingress_path=row[9],
            )

    def list_status(self) -> list[AppStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, live_replicas, image, created, updated, removed, ingress_host, ingress_path
                FROM app_status ORDER BY app_name
                """
            ).fetchall()
        return [
            AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                live_replicas=row[3],
                image=row[4],
                created=row[5],
                updated=row[6],
                removed=row[7],
                ingress_host=row[8],
                ingress_path=row[9],
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
