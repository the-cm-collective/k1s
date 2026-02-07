"""SQLite-backed spool for gateway durability (Option A)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class InflightRecord:
    work_id: str
    attempt: int
    js_stream: str
    js_consumer: str
    js_seq: int
    received_at: str
    node_id: str | None
    state: str
    last_progress_at: str | None


@dataclass(slots=True)
class ResultRecord:
    work_id: str
    attempt: int
    status: str
    payload: dict
    committed_at: str
    delivered_to_controller_at: str | None


class GatewaySpool:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inflight (
                  work_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,
                  js_stream TEXT NOT NULL,
                  js_consumer TEXT NOT NULL,
                  js_seq INTEGER NOT NULL,
                  received_at TEXT NOT NULL,
                  node_id TEXT,
                  state TEXT NOT NULL,
                  last_progress_at TEXT,
                  PRIMARY KEY (work_id, attempt)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                  work_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  committed_at TEXT NOT NULL,
                  delivered_to_controller_at TEXT,
                  PRIMARY KEY (work_id, attempt)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS inflight_state_idx ON inflight(state)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS results_delivered_idx ON results(delivered_to_controller_at)"
            )
            conn.commit()

    def record_inflight(
        self,
        *,
        work_id: str,
        attempt: int,
        js_stream: str,
        js_consumer: str,
        js_seq: int,
        node_id: str | None,
        state: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inflight
                  (work_id, attempt, js_stream, js_consumer, js_seq, received_at,
                   node_id, state, last_progress_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, attempt) DO UPDATE SET
                  js_stream = excluded.js_stream,
                  js_consumer = excluded.js_consumer,
                  js_seq = excluded.js_seq,
                  node_id = excluded.node_id,
                  state = excluded.state,
                  last_progress_at = excluded.last_progress_at
                """,
                (
                    work_id,
                    int(attempt),
                    js_stream,
                    js_consumer,
                    int(js_seq),
                    now,
                    node_id,
                    state,
                    now,
                ),
            )
            conn.commit()

    def update_inflight_state(
        self, work_id: str, attempt: int, state: str, node_id: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inflight
                SET state = ?, node_id = COALESCE(?, node_id), last_progress_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (state, node_id, now, work_id, int(attempt)),
            )
            conn.commit()

    def get_inflight_state(self, work_id: str, attempt: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state
                FROM inflight
                WHERE work_id = ? AND attempt = ?
                """,
                (work_id, int(attempt)),
            ).fetchone()
            if not row:
                return None
            return str(row[0])

    def record_result(self, work_id: str, attempt: int, status: str, payload: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO results
                  (work_id, attempt, status, payload_json, committed_at, delivered_to_controller_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, attempt) DO UPDATE SET
                  status = excluded.status,
                  payload_json = excluded.payload_json,
                  committed_at = excluded.committed_at
                """,
                (work_id, int(attempt), status, payload_json, now, None),
            )
            conn.commit()

    def get_result(self, work_id: str, attempt: int) -> ResultRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT work_id, attempt, status, payload_json, committed_at,
                       delivered_to_controller_at
                FROM results
                WHERE work_id = ? AND attempt = ?
                """,
                (work_id, int(attempt)),
            ).fetchone()
            if not row:
                return None
            try:
                payload = json.loads(row[3]) if row[3] else {}
            except Exception:
                payload = {}
            return ResultRecord(
                work_id=str(row[0]),
                attempt=int(row[1]),
                status=str(row[2]),
                payload=payload,
                committed_at=str(row[4]),
                delivered_to_controller_at=str(row[5]) if row[5] else None,
            )

    def list_undelivered_results(self, limit: int = 100) -> list[ResultRecord]:
        rows: list[ResultRecord] = []
        with self._connect() as conn:
            results = conn.execute(
                """
                SELECT work_id, attempt, status, payload_json, committed_at,
                       delivered_to_controller_at
                FROM results
                WHERE delivered_to_controller_at IS NULL
                ORDER BY committed_at
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            for row in results:
                try:
                    payload = json.loads(row[3]) if row[3] else {}
                except Exception:
                    payload = {}
                rows.append(
                    ResultRecord(
                        work_id=str(row[0]),
                        attempt=int(row[1]),
                        status=str(row[2]),
                        payload=payload,
                        committed_at=str(row[4]),
                        delivered_to_controller_at=str(row[5]) if row[5] else None,
                    )
                )
        return rows

    def mark_result_delivered(self, work_id: str, attempt: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE results
                SET delivered_to_controller_at = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (now, work_id, int(attempt)),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def cleanup(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self._path if not suffix else Path(f"{self._path}{suffix}")
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


__all__ = ["GatewaySpool", "InflightRecord", "ResultRecord"]
