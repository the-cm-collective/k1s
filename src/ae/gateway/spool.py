"""SQLite-backed spool for gateway durability (Option A)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ae.ha.fencing import parse_envelope


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
    controller_id: str | None
    controller_epoch: int | None
    operation_id: str | None


@dataclass(slots=True)
class ResultRecord:
    work_id: str
    attempt: int
    status: str
    payload: dict
    committed_at: str
    delivered_to_controller_at: str | None
    replay_attempts: int
    last_replay_at: str | None
    next_retry_at: str | None
    last_replay_error: str | None
    controller_id: str | None
    controller_epoch: int | None
    operation_id: str | None


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
                  controller_id TEXT,
                  controller_epoch INTEGER,
                  operation_id TEXT,
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
                  replay_attempts INTEGER NOT NULL DEFAULT 0,
                  last_replay_at TEXT,
                  next_retry_at TEXT,
                  last_replay_error TEXT,
                  controller_id TEXT,
                  controller_epoch INTEGER,
                  operation_id TEXT,
                  PRIMARY KEY (work_id, attempt)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS inflight_state_idx ON inflight(state)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS results_delivered_idx ON results(delivered_to_controller_at)"
            )
            self._ensure_column(conn, "inflight", "controller_id", "TEXT")
            self._ensure_column(conn, "inflight", "controller_epoch", "INTEGER")
            self._ensure_column(conn, "inflight", "operation_id", "TEXT")
            self._ensure_column(conn, "results", "controller_id", "TEXT")
            self._ensure_column(conn, "results", "controller_epoch", "INTEGER")
            self._ensure_column(conn, "results", "operation_id", "TEXT")
            self._ensure_column(conn, "results", "replay_attempts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "results", "last_replay_at", "TEXT")
            self._ensure_column(conn, "results", "next_retry_at", "TEXT")
            self._ensure_column(conn, "results", "last_replay_error", "TEXT")
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
        payload: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        envelope = parse_envelope(payload or {})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inflight
                  (work_id, attempt, js_stream, js_consumer, js_seq, received_at,
                   node_id, state, last_progress_at, controller_id, controller_epoch, operation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, attempt) DO UPDATE SET
                  js_stream = excluded.js_stream,
                  js_consumer = excluded.js_consumer,
                  js_seq = excluded.js_seq,
                  node_id = excluded.node_id,
                  state = excluded.state,
                  last_progress_at = excluded.last_progress_at,
                  controller_id = excluded.controller_id,
                  controller_epoch = excluded.controller_epoch,
                  operation_id = excluded.operation_id
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
                    envelope.controller_id if envelope is not None else None,
                    envelope.controller_epoch if envelope is not None else None,
                    envelope.operation_id if envelope is not None else None,
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

    def get_inflight_record(self, work_id: str, attempt: int) -> InflightRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT work_id, attempt, js_stream, js_consumer, js_seq, received_at,
                       node_id, state, last_progress_at, controller_id,
                       controller_epoch, operation_id
                FROM inflight
                WHERE work_id = ? AND attempt = ?
                """,
                (work_id, int(attempt)),
            ).fetchone()
            if not row:
                return None
            return InflightRecord(
                work_id=str(row[0]),
                attempt=int(row[1]),
                js_stream=str(row[2]),
                js_consumer=str(row[3]),
                js_seq=int(row[4]),
                received_at=str(row[5]),
                node_id=str(row[6]) if row[6] else None,
                state=str(row[7]),
                last_progress_at=str(row[8]) if row[8] else None,
                controller_id=str(row[9]) if row[9] else None,
                controller_epoch=int(row[10]) if row[10] is not None else None,
                operation_id=str(row[11]) if row[11] else None,
            )

    def record_result(self, work_id: str, attempt: int, status: str, payload: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        envelope = parse_envelope(payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO results
                  (work_id, attempt, status, payload_json, committed_at, delivered_to_controller_at,
                   replay_attempts, last_replay_at, next_retry_at, last_replay_error,
                   controller_id, controller_epoch, operation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, attempt) DO UPDATE SET
                  status = excluded.status,
                  payload_json = excluded.payload_json,
                  committed_at = excluded.committed_at,
                  delivered_to_controller_at = excluded.delivered_to_controller_at,
                  replay_attempts = excluded.replay_attempts,
                  last_replay_at = excluded.last_replay_at,
                  next_retry_at = excluded.next_retry_at,
                  last_replay_error = excluded.last_replay_error,
                  controller_id = excluded.controller_id,
                  controller_epoch = excluded.controller_epoch,
                  operation_id = excluded.operation_id
                """,
                (
                    work_id,
                    int(attempt),
                    status,
                    payload_json,
                    now,
                    None,
                    0,
                    None,
                    None,
                    None,
                    envelope.controller_id if envelope is not None else None,
                    envelope.controller_epoch if envelope is not None else None,
                    envelope.operation_id if envelope is not None else None,
                ),
            )
            conn.commit()

    def get_result(self, work_id: str, attempt: int) -> ResultRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT work_id, attempt, status, payload_json, committed_at,
                       delivered_to_controller_at, replay_attempts, last_replay_at,
                       next_retry_at, last_replay_error, controller_id,
                       controller_epoch, operation_id
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
                replay_attempts=int(row[6] or 0),
                last_replay_at=str(row[7]) if row[7] else None,
                next_retry_at=str(row[8]) if row[8] else None,
                last_replay_error=str(row[9]) if row[9] else None,
                controller_id=str(row[10]) if row[10] else None,
                controller_epoch=int(row[11]) if row[11] is not None else None,
                operation_id=str(row[12]) if row[12] else None,
            )

    def list_undelivered_results(self, limit: int = 100) -> list[ResultRecord]:
        rows: list[ResultRecord] = []
        with self._connect() as conn:
            results = conn.execute(
                """
                SELECT work_id, attempt, status, payload_json, committed_at,
                       delivered_to_controller_at, replay_attempts, last_replay_at,
                       next_retry_at, last_replay_error, controller_id,
                       controller_epoch, operation_id
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
                            replay_attempts=int(row[6] or 0),
                            last_replay_at=str(row[7]) if row[7] else None,
                            next_retry_at=str(row[8]) if row[8] else None,
                            last_replay_error=str(row[9]) if row[9] else None,
                            controller_id=str(row[10]) if row[10] else None,
                            controller_epoch=int(row[11]) if row[11] is not None else None,
                            operation_id=str(row[12]) if row[12] else None,
                        )
                    )
        return rows

    def list_replay_ready_results(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[ResultRecord]:
        when = (now or datetime.now(timezone.utc)).isoformat()
        rows: list[ResultRecord] = []
        with self._connect() as conn:
            results = conn.execute(
                """
                SELECT work_id, attempt, status, payload_json, committed_at,
                       delivered_to_controller_at, replay_attempts, last_replay_at,
                       next_retry_at, last_replay_error, controller_id,
                       controller_epoch, operation_id
                FROM results
                WHERE delivered_to_controller_at IS NULL
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY COALESCE(next_retry_at, committed_at), committed_at
                LIMIT ?
                """,
                (when, int(limit)),
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
                        replay_attempts=int(row[6] or 0),
                        last_replay_at=str(row[7]) if row[7] else None,
                        next_retry_at=str(row[8]) if row[8] else None,
                        last_replay_error=str(row[9]) if row[9] else None,
                        controller_id=str(row[10]) if row[10] else None,
                        controller_epoch=int(row[11]) if row[11] is not None else None,
                        operation_id=str(row[12]) if row[12] else None,
                    )
                )
        return rows

    def count_undelivered_results(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM results
                WHERE delivered_to_controller_at IS NULL
                """
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def mark_result_delivered(self, work_id: str, attempt: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE results
                SET delivered_to_controller_at = ?,
                    next_retry_at = NULL,
                    last_replay_error = NULL
                WHERE work_id = ? AND attempt = ?
                """,
                (now, work_id, int(attempt)),
            )
            conn.commit()

    def record_result_delivery_attempt(
        self,
        work_id: str,
        attempt: int,
        *,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        retry_at_text = retry_at.isoformat() if retry_at is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE results
                SET replay_attempts = replay_attempts + 1,
                    last_replay_at = ?,
                    next_retry_at = ?,
                    last_replay_error = ?
                WHERE work_id = ? AND attempt = ?
                """,
                (now, retry_at_text, error, work_id, int(attempt)),
            )
            conn.commit()

    def reset_replay_schedule(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE results
                SET next_retry_at = NULL
                WHERE delivered_to_controller_at IS NULL
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(row[1]) for row in rows}
        if column in names:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def cleanup(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self._path if not suffix else Path(f"{self._path}{suffix}")
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


__all__ = ["GatewaySpool", "InflightRecord", "ResultRecord"]
