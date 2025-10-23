"""State persistence helpers backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ae.controller.spec import AppManifest


@dataclass(slots=True)
class AppStatus:
    """Latest reconcile snapshot for an application."""

    app_name: str
    desired_replicas: int
    ready_replicas: int
    image: str


class SQLiteStateStore:
    """Minimal SQLite-backed store for reconcile snapshots."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_status (
                    app_name TEXT PRIMARY KEY,
                    desired_replicas INTEGER NOT NULL,
                    ready_replicas INTEGER NOT NULL,
                    image TEXT NOT NULL,
                    replica_meta TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def record_snapshot(
        self,
        manifest: AppManifest,
        ready_replicas: int,
        replica_meta: Sequence[str],
    ) -> None:
        payload = json.dumps(list(replica_meta))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_status(app_name, desired_replicas, ready_replicas, image, replica_meta)
                VALUES(?,?,?,?,?)
                ON CONFLICT(app_name) DO UPDATE SET
                    desired_replicas=excluded.desired_replicas,
                    ready_replicas=excluded.ready_replicas,
                    image=excluded.image,
                    replica_meta=excluded.replica_meta
                """,
                (
                    manifest.metadata.name,
                    manifest.spec.replicas,
                    ready_replicas,
                    manifest.spec.image,
                    payload,
                ),
            )
            conn.commit()

    def get_status(self, app_name: str) -> AppStatus | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT app_name, desired_replicas, ready_replicas, image FROM app_status WHERE app_name = ?",
                (app_name,),
            ).fetchone()
            if row is None:
                return None
            return AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                image=row[3],
            )

    def list_status(self) -> list[AppStatus]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT app_name, desired_replicas, ready_replicas, image FROM app_status ORDER BY app_name"
            ).fetchall()
        return [
            AppStatus(
                app_name=row[0],
                desired_replicas=row[1],
                ready_replicas=row[2],
                image=row[3],
            )
            for row in rows
        ]
