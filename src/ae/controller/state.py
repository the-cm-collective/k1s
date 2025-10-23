"""State persistence helpers backed by SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
    image: str
    created: int
    updated: int
    removed: int


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
                    "image",
                    "created",
                    "updated",
                    "removed",
                ],
            ):
                conn.execute("DROP TABLE IF EXISTS replica_status")
                conn.execute("DROP TABLE IF EXISTS app_status")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_status (
                    app_name TEXT PRIMARY KEY,
                    desired_replicas INTEGER NOT NULL,
                    ready_replicas INTEGER NOT NULL,
                    image TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    removed INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS replica_status (
                    app_name TEXT NOT NULL,
                    replica_id TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    PRIMARY KEY (app_name, replica_id)
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
                INSERT INTO app_status(app_name, desired_replicas, ready_replicas, image, created, updated, removed)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    desired_replicas=excluded.desired_replicas,
                    ready_replicas=excluded.ready_replicas,
                    image=excluded.image,
                    created=excluded.created,
                    updated=excluded.updated,
                    removed=excluded.removed
                """,
                (
                    manifest.metadata.name,
                    manifest.spec.replicas,
                    health_report.ready_replicas,
                    manifest.spec.image,
                    runtime_result.created,
                    runtime_result.updated,
                    runtime_result.removed,
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
                    state_by_id.get(replica.replica_id, None).status
                    if state_by_id.get(replica.replica_id)
                    else "unknown",
                    replica.message,
                )
                for replica in health_report.replicas
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO replica_status(app_name, replica_id, ready, status, message)
                    VALUES(?,?,?,?,?)
                    """,
                    rows,
                )
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, image, created, updated, removed
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
                image=row[3],
                created=row[4],
                updated=row[5],
                removed=row[6],
            )

    def list_status(self) -> list[AppStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_name, desired_replicas, ready_replicas, image, created, updated, removed
                FROM app_status ORDER BY app_name
                """
            ).fetchall()
        return [
            AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                image=row[3],
                created=row[4],
                updated=row[5],
                removed=row[6],
            )
            for row in rows
        ]
